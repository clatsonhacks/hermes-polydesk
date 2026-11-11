"""Read-only Base Sepolia (Coinbase's L2 testnet, chain 84532) balance reader.

Used to back the paper bankroll with real testnet funds: you can only deposit as much
paper USDC as the linked wallet actually holds in testnet USDC. Never signs, never
sends a transaction, never asks for a key.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_RPC = "https://sepolia.base.org"
CHAIN_ID = 84532
# Circle's official testnet USDC on Base Sepolia (faucet: https://faucet.circle.com).
USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
USDC_DECIMALS = 6
EXPLORER = "https://sepolia.basescan.org/address/"

_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")


class WalletError(Exception):
    pass


def valid_address(addr: str) -> bool:
    return bool(_ADDR.fullmatch((addr or "").strip()))


def _rpc(url: str, method: str, params: list):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise WalletError("rpc_url must be an https URL")
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "User-Agent": "hermes-polydesk/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read(1_000_000).decode())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise WalletError(f"could not reach Base Sepolia RPC {parsed.hostname} ({exc})") from exc
    if not isinstance(data, dict) or "error" in data:
        raise WalletError(f"RPC error: {data.get('error') if isinstance(data, dict) else data}")
    return data.get("result")


def balances(address: str, rpc_url: str = DEFAULT_RPC) -> dict:
    if not valid_address(address):
        raise WalletError("not a valid 0x… EVM address")
    address = address.strip()
    chain = int(_rpc(rpc_url, "eth_chainId", []), 16)
    if chain != CHAIN_ID:
        raise WalletError(f"rpc_url is chain {chain}, expected Base Sepolia ({CHAIN_ID})")
    eth_wei = int(_rpc(rpc_url, "eth_getBalance", [address, "latest"]), 16)
    call = {"to": USDC, "data": "0x70a08231" + address[2:].lower().rjust(64, "0")}
    raw = _rpc(rpc_url, "eth_call", [call, "latest"]) or "0x0"
    usdc = int(raw, 16) if raw not in ("0x", "") else 0
    return {
        "network": "base-sepolia",
        "chain_id": CHAIN_ID,
        "address": address,
        "eth": eth_wei / 1e18,
        "usdc": usdc / 10 ** USDC_DECIMALS,
        "explorer": EXPLORER + address,
    }
