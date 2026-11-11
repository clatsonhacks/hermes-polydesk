"""Offline tests: Polymarket and the RPC are mocked. Run: python -m pytest tests -q"""

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("polydesk", ROOT / "__init__.py",
                                              submodule_search_locations=[str(ROOT)])
polydesk = importlib.util.module_from_spec(spec)
sys.modules["polydesk"] = polydesk
spec.loader.exec_module(polydesk)

from polydesk import desk as dk, polymarket as pm, tools, wallet as wl  # noqa: E402

MARKET = {
    "id": "1", "slug": "will-x-happen", "question": "Will X happen?", "url": "https://polymarket.com/event/x",
    "outcomes": [{"name": "Yes", "price": 0.40, "token_id": "TYES"},
                 {"name": "No", "price": 0.60, "token_id": "TNO"}],
    "volume_24h": 1e5, "liquidity": 5e4, "end_date": None,
    "closed": False, "active": True, "accepting_orders": True, "order_book": True,
}
BOOKS = {
    "TYES": {"bids": [(0.39, 500), (0.38, 1000)], "asks": [(0.40, 200), (0.41, 300), (0.50, 10000)]},
    "TNO": {"bids": [(0.59, 500)], "asks": [(0.61, 500)]},
}


class FakeCtx:
    def __init__(self, cfg=None):
        self.cfg = cfg or {}
        self.tools, self.commands, self.skills = {}, {}, {}

    def get_config(self, key, default=None):
        return self.cfg.get(key, default)

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = handler

    def register_command(self, name, handler, **kw):
        self.commands[name] = handler

    def register_skill(self, name, path, **kw):
        self.skills[name] = path


def setup(monkeypatch=None, cfg=None):
    state = {"market": json.loads(json.dumps(MARKET))}
    pm.get_market = lambda ref, fresh=False: json.loads(json.dumps(state["market"]))
    pm.order_book = lambda tid: {**BOOKS[tid], "tick_size": 0.01, "min_order_size": 5}
    pm.clear_cache = lambda: None
    ctx = FakeCtx(cfg)
    h = tools.Handlers(ctx, db_path=Path(tempfile.mkdtemp()) / "t.db")
    return h, state, ctx


def call(fn, **args):
    return json.loads(fn(args))


def test_register():
    ctx = FakeCtx()
    polydesk.register(ctx)
    assert set(ctx.tools) == {"polydesk_markets", "polydesk_trade", "polydesk_portfolio",
                              "polydesk_history", "polydesk_wallet"}
    assert "desk" in ctx.commands and "polydesk" in ctx.skills


def test_fill_engine_sweeps_levels():
    f = dk.simulate_buy([(0.40, 200), (0.41, 300)], 100)
    assert abs(f["shares"] - (200 + 20 / 0.41)) < 1e-9 and f["levels"] == 2
    s = dk.simulate_sell([(0.39, 500), (0.38, 1000)], 600)
    assert abs(s["usd"] - (500 * 0.39 + 100 * 0.38)) < 1e-9


def test_preview_confirm_and_portfolio():
    h, _, _ = setup()
    q = call(h.trade, market="will-x-happen", outcome="yes", side="buy", amount_usd=100)
    assert q["status"] == "awaiting_confirmation" and q["ticket_id"]
    assert q["book_levels_swept"] == 2
    r = call(h.trade, ticket_id=q["ticket_id"])
    assert r["status"] == "filled" and abs(r["cash_after"] - 900) < 0.01
    again = call(h.trade, ticket_id=q["ticket_id"])
    assert "error" in again  # tickets are single use
    p = call(h.portfolio)
    pos = p["positions"][0]
    assert pos["outcome"] == "Yes" and abs(pos["mid"] - 0.395) < 1e-9
    assert p["total_pnl_usd"] < 0  # marked at mid, below entry


def test_risk_limits():
    h, _, _ = setup(cfg={"max_trade_usd": 50})
    r = call(h.trade, market="x", outcome="Yes", side="buy", amount_usd=100)
    assert r["status"] == "rejected" and any("max_trade_usd" in p for p in r["problems"])
    h, _, _ = setup()
    r = call(h.trade, market="x", outcome="Yes", side="buy", amount_usd=240)  # sweeps to 0.50
    assert r["status"] == "rejected" and any("sweep" in p for p in r["problems"])
    r = call(h.trade, market="x", outcome="Maybe", side="buy", amount_usd=10)
    assert "error" in r


def test_sell_realizes_pnl():
    h, _, _ = setup(cfg={"auto_execute": True})
    call(h.trade, market="x", outcome="Yes", side="buy", amount_usd=80)  # 200 sh @ .40
    r = call(h.trade, market="x", outcome="Yes", side="sell")  # sell all @ .39
    assert r["status"] == "filled" and abs(r["realized_pnl"] - (200 * 0.39 - 80)) < 0.01
    assert call(h.portfolio)["positions"] == []


def test_settlement_pays_winners():
    h, state, _ = setup(cfg={"auto_execute": True})
    call(h.trade, market="x", outcome="Yes", side="buy", amount_usd=80)
    state["market"]["closed"] = True
    state["market"]["outcomes"][0]["price"] = 1.0
    state["market"]["outcomes"][1]["price"] = 0.0
    p = call(h.portfolio)
    assert p["settled_now"][0]["result"] == "WON"
    assert abs(p["cash_usd"] - (1000 - 80 + 200)) < 0.01 and p["positions"] == []


def test_wallet_backed_deposit():
    h, _, _ = setup(cfg={"demo_bankroll": 0})
    addr = "0x" + "ab" * 20
    wl.balances = lambda a, rpc=None: {"address": a, "usdc": 20.0, "eth": 0.1, "explorer": "e",
                                       "network": "base-sepolia", "chain_id": 84532}
    assert call(h.wallet, action="link", address=addr)["linked"]
    assert "error" in call(h.wallet, action="link", address="0x123")
    d = call(h.wallet, action="deposit", amount_usd=15)
    assert d["cash_usd"] == 15 and d["testnet_backed_usd"] == 15
    assert "at most 5.00" in call(h.wallet, action="deposit", amount_usd=10)["error"]
    p = call(h.portfolio)
    assert p["bankroll"]["testnet_backed_usd"] == 15 and p["bankroll"]["unbacked_demo_usd"] == 0


def test_command_renders():
    h, _, _ = setup(cfg={"auto_execute": True})
    call(h.trade, market="x", outcome="Yes", side="buy", amount_usd=40)
    out = h.command("")
    assert "Polydesk" in out and "Will X happen?" in out
    assert "confirm" in h.command("reset")
    assert "reset" in h.command("reset confirm").lower()
