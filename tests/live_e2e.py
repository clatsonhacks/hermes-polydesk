"""Live end-to-end test through a real Hermes install.

Loads the plugin with Hermes' own discovery, then calls every tool via
model_tools.handle_function_call (the same path the agent uses) against the LIVE
Polymarket API and Base Sepolia RPC. Run with the Hermes runtime's Python, with
HERMES_HOME pointing at a profile where polydesk is installed and enabled.
Not collected by pytest (no test_ prefix).
"""

from __future__ import annotations

import json
import os
import sys

# A Base Sepolia contract that holds a little testnet USDC: a stable, public read target.
TESTNET_ADDR = os.environ.get("POLYDESK_TEST_ADDR", "0x4200000000000000000000000000000000000016")
STEPS: list[tuple[str, bool, str]] = []


def step(name: str, ok: bool, detail: str = "") -> None:
    STEPS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)
    if not ok:
        finish(1)


def finish(code: int) -> None:
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    lines = ["## Polydesk live end-to-end", "", "| | step | detail |", "|---|---|---|"]
    lines += [f"| {'✅' if ok else '❌'} | {n} | {d.replace('|', '/')[:200]} |" for n, ok, d in STEPS]
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    for n, ok, d in STEPS:  # annotations are readable on the public run page
        print(f"::{'notice' if ok else 'error'} title={'PASS' if ok else 'FAIL'} {n}::{d[:300]}")
    sys.exit(code)


def main() -> None:
    from hermes_cli.plugins import discover_plugins
    from model_tools import get_tool_definitions, handle_function_call

    discover_plugins(force=True)
    names = {t.get("function", t).get("name") for t in get_tool_definitions(quiet_mode=True)}
    want = {"polydesk_markets", "polydesk_trade", "polydesk_portfolio", "polydesk_history", "polydesk_wallet"}
    step("Hermes discovers all 5 tools", want <= names, ", ".join(sorted(want & names)))

    def call(tool: str, **args) -> dict:
        raw = handle_function_call(tool, args, task_id="e2e", session_id="e2e")
        print(f"   {tool}({json.dumps(args)}) -> {raw[:400]}", flush=True)
        try:
            return json.loads(raw)
        except ValueError:
            return {"error": f"non-JSON result: {raw[:200]}"}

    # --- market discovery (live Polymarket)
    trending = call("polydesk_markets", limit=20)
    step("Trending markets load from Polymarket", trending.get("count", 0) > 0,
         f"{trending.get('count')} markets" if "error" not in trending else trending["error"])
    search = call("polydesk_markets", query="bitcoin", limit=5)
    step("Search works", "error" not in search and search.get("count", 0) > 0, f"{search.get('count')} results for 'bitcoin'")

    candidates = [m for m in trending["markets"]
                  if [o["name"] for o in m["outcomes"]] == ["Yes", "No"]
                  and 0.10 <= (m["outcomes"][0]["price"] or 0) <= 0.90]
    step("Found a liquid Yes/No market", bool(candidates), candidates[0]["question"] if candidates else "")

    # --- quote -> confirm -> fill, trying markets until one has a tight book
    filled = None
    for m in candidates[:8]:
        detail = call("polydesk_markets", market=m["slug"])
        yes = detail.get("market", {}).get("outcomes", [{}])[0]
        if yes.get("best_ask") is None:
            continue
        q = call("polydesk_trade", market=m["slug"], outcome="Yes", side="buy", amount_usd=5)
        if q.get("status") != "awaiting_confirmation":
            continue
        step("Order book read", True, f"{m['question'][:60]}: bid {yes.get('best_bid')} / ask {yes.get('best_ask')}")
        step("Quote returns a confirm ticket", bool(q.get("ticket_id")),
             f"{q['shares']} sh @ {q['avg_price']} for ${q['usd']} ({q['book_levels_swept']} levels)")
        f = call("polydesk_trade", ticket_id=q["ticket_id"])
        step("Ticket executes against live book", f.get("status") == "filled",
             f"cash after ${f.get('cash_after')}" if f.get("status") == "filled" else json.dumps(f)[:200])
        reuse = call("polydesk_trade", ticket_id=q["ticket_id"])
        step("Ticket is single-use", "error" in reuse, reuse.get("error", ""))
        filled = m
        break
    step("A $5 paper buy filled on some market", filled is not None)

    big = call("polydesk_trade", market=filled["slug"], outcome="Yes", side="buy", amount_usd=5000)
    step("Oversized trade is rejected by risk limits", big.get("status") == "rejected",
         "; ".join(big.get("problems", [])) or big.get("error", ""))
    bad = call("polydesk_trade", market=filled["slug"], outcome="Maybe", side="buy", amount_usd=5)
    step("Unknown outcome gives a clear error", "error" in bad, bad.get("error", ""))

    pf = call("polydesk_portfolio")
    pos = (pf.get("positions") or [{}])[0]
    step("Portfolio marks position to live mid", len(pf.get("positions", [])) == 1 and pos.get("mid") is not None,
         f"{pos.get('shares')} sh, mid {pos.get('mid')}, uPnL {pos.get('unrealized_pnl')}, equity {pf.get('equity_usd')}")

    sq = call("polydesk_trade", market=filled["slug"], outcome="Yes", side="sell")
    if sq.get("status") == "awaiting_confirmation":
        sq = call("polydesk_trade", ticket_id=sq["ticket_id"])
    step("Sell-all closes the position with realized P&L", sq.get("status") == "filled",
         f"realized {sq.get('realized_pnl')}" if sq.get("status") == "filled" else json.dumps(sq)[:200])
    pf2 = call("polydesk_portfolio")
    step("Portfolio is flat after selling", pf2.get("positions") == [], f"equity {pf2.get('equity_usd')}")

    hist = call("polydesk_history", limit=10)
    kinds = [h["kind"] for h in hist.get("activity", [])]
    step("History records deposit, buy and sell", {"DEPOSIT", "BUY", "SELL"} <= set(kinds), ", ".join(kinds))

    # --- Base Sepolia testnet wallet (live RPC)
    nolink = call("polydesk_wallet", action="status")
    step("Wallet status without a link explains what to do", "error" in nolink, nolink.get("error", ""))
    bad_addr = call("polydesk_wallet", action="link", address="0x1234")
    step("Invalid address rejected", "error" in bad_addr, bad_addr.get("error", ""))
    link = call("polydesk_wallet", action="link", address=TESTNET_ADDR)
    step("Link reads live Base Sepolia balances", link.get("linked") is True and link.get("chain_id") == 84532,
         f"ETH {link.get('eth')}, USDC {link.get('usdc')}")
    usdc = float(link.get("usdc") or 0)
    if usdc > 0:
        amt = round(min(usdc / 2, 1.0), 2) or 0.01
        dep = call("polydesk_wallet", action="deposit", amount_usd=amt)
        step("Backed deposit credited", "error" not in dep, f"deposited {dep.get('deposited')}, backed {dep.get('testnet_backed_usd')}")
        over = call("polydesk_wallet", action="deposit", amount_usd=usdc * 10 + 1)
        step("Deposit capped at on-chain balance", "error" in over, over.get("error", ""))
    st = call("polydesk_wallet", action="status")
    step("Wallet status shows bankroll backing", "paper_bankroll" in st, json.dumps(st.get("paper_bankroll")))
    finish(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # report crashes as a failed step
        import traceback
        traceback.print_exc()
        STEPS.append(("Unhandled exception", False, repr(exc)))
        finish(1)
