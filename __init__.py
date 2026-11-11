"""Polydesk: an agent paper-trading desk for Polymarket, backed by a Base Sepolia testnet wallet."""

from __future__ import annotations

import logging
from pathlib import Path

from . import schemas
from .tools import Handlers

logger = logging.getLogger(__name__)
TOOLSET = "polydesk"


def register(ctx):
    h = Handlers(ctx)
    handlers = {
        "polydesk_markets": h.markets,
        "polydesk_trade": h.trade,
        "polydesk_portfolio": h.portfolio,
        "polydesk_history": h.history,
        "polydesk_wallet": h.wallet,
    }
    for schema in schemas.ALL:
        ctx.register_tool(name=schema["name"], toolset=TOOLSET, schema=schema,
                          handler=handlers[schema["name"]])

    ctx.register_command("desk", h.command,
                         description="Polydesk paper portfolio, history and reset",
                         args_hint="[history|reset]")

    skill_md = Path(__file__).parent / "skills" / "polydesk" / "SKILL.md"
    if skill_md.exists():
        try:
            ctx.register_skill("polydesk", skill_md,
                               description="Research Polymarket markets and paper-trade them safely with Polydesk.")
        except Exception as exc:  # skill registration must never disable the tools
            logger.warning("polydesk: skill not registered (%s)", exc)
