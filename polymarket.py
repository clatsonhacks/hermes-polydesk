"""Keyless Polymarket client: Gamma (market metadata) and CLOB (live order books).

Standard library only. HTTPS GET only, to an explicit host allowlist. Never signs,
never places a real order, never needs a key.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
_ALLOWED_HOSTS = frozenset({"gamma-api.polymarket.com", "clob.polymarket.com"})
_UA = "hermes-polydesk/1.0"
_MAX_BYTES = 4_000_000

_cache: dict[str, tuple[float, Any]] = {}
_lock = threading.Lock()


class PolymarketError(Exception):
    """Network or data problem; the message is safe to show the user."""


def _get(base: str, path: str, params: Optional[dict] = None, ttl: float = 5.0) -> Any:
    clean = {}
    for k, v in (params or {}).items():
        if v is None:
            continue
        clean[k] = ("true" if v else "false") if isinstance(v, bool) else str(v)
    url = f"{base}{path}" + (f"?{urllib.parse.urlencode(clean)}" if clean else "")
    if urllib.parse.urlparse(url).hostname not in _ALLOWED_HOSTS:
        raise PolymarketError("blocked: host is not on the allowlist")

    now = time.monotonic()
    with _lock:
        hit = _cache.get(url)
        if hit and ttl > 0 and now - hit[0] < ttl:
            return hit[1]

    last: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read(_MAX_BYTES + 1)
            if len(raw) > _MAX_BYTES:
                raise PolymarketError("Polymarket response was too large")
            data = json.loads(raw.decode("utf-8"))
            with _lock:
                _cache[url] = (now, data)
                if len(_cache) > 128:
                    _cache.pop(next(iter(_cache)))
            return data
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise PolymarketError("Polymarket has no data for that market/token") from exc
            last = exc
            if exc.code not in (429, 500, 502, 503, 504):
                break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PolymarketError("Polymarket sent data that could not be read") from exc
        time.sleep(0.5 * (attempt + 1))
    reason = getattr(last, "reason", None) or getattr(last, "code", None) or last
    raise PolymarketError(f"could not reach Polymarket ({reason})")


# ----------------------------------------------------------------------------- parsing

def _jlist(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            out = json.loads(value)
            return out if isinstance(out, list) else []
        except ValueError:
            return []
    return []


def num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def normalize_market(raw: dict, event: Optional[dict] = None) -> Optional[dict]:
    if not isinstance(raw, dict) or not raw.get("question"):
        return None
    outcomes = [str(o) for o in _jlist(raw.get("outcomes"))]
    prices = [num(p) for p in _jlist(raw.get("outcomePrices"))]
    tokens = [str(t) for t in _jlist(raw.get("clobTokenIds"))]
    if event is None:
        evs = raw.get("events")
        if isinstance(evs, list) and evs and isinstance(evs[0], dict):
            event = evs[0]
    slug = str(raw.get("slug") or "")
    if event and event.get("slug"):
        url = f"https://polymarket.com/event/{event['slug']}"
    else:
        url = f"https://polymarket.com/market/{slug}" if slug else ""
    return {
        "id": str(raw.get("id") or ""),
        "slug": slug,
        "question": str(raw["question"]),
        "url": url,
        "outcomes": [
            {"name": outcomes[i],
             "price": prices[i] if i < len(prices) else None,
             "token_id": tokens[i] if i < len(tokens) else None}
            for i in range(len(outcomes))
        ],
        "volume_24h": num(raw.get("volume24hr")) or 0.0,
        "liquidity": num(raw.get("liquidityNum")) or num(raw.get("liquidity")) or 0.0,
        "end_date": raw.get("endDate") or raw.get("endDateIso"),
        "closed": bool(raw.get("closed")),
        "active": raw.get("active") is not False,
        "accepting_orders": raw.get("acceptingOrders") is not False,
        "order_book": raw.get("enableOrderBook") is not False,
    }


# ----------------------------------------------------------------------------- endpoints

def search_markets(query: str, limit: int = 8) -> list[dict]:
    data = _get(GAMMA, "/public-search", {"q": query, "limit_per_type": max(1, min(limit, 20)),
                                           "events_status": "active"}, ttl=30)
    out, seen = [], set()
    for ev in (data.get("events") if isinstance(data, dict) else None) or []:
        if not isinstance(ev, dict):
            continue
        for raw in ev.get("markets") or []:
            m = normalize_market(raw, event=ev)
            if m and not m["closed"] and m["active"] and m["slug"] not in seen:
                seen.add(m["slug"])
                out.append(m)
    out.sort(key=lambda m: m["volume_24h"], reverse=True)
    return out[:limit]


def trending(limit: int = 8) -> list[dict]:
    data = _get(GAMMA, "/markets", {"closed": False, "active": True, "order": "volume24hr",
                                    "ascending": False, "limit": max(1, min(limit, 50))}, ttl=30)
    return [m for m in (normalize_market(r) for r in data or []) if m]


def slug_from_ref(ref: str) -> str:
    ref = (ref or "").strip()
    if ref.startswith("http"):
        ref = urllib.parse.urlparse(ref).path.rstrip("/").rsplit("/", 1)[-1]
    return ref


def get_market(ref: str, fresh: bool = False) -> Optional[dict]:
    """Market by slug, numeric id or polymarket.com/market URL (open or closed)."""
    ref = slug_from_ref(ref)
    if not ref:
        return None
    key = "id" if ref.isdigit() else "slug"
    ttl = 0 if fresh else 15
    data = _get(GAMMA, "/markets", {key: ref}, ttl=ttl)
    if not data:
        data = _get(GAMMA, "/markets", {key: ref, "closed": True}, ttl=ttl)
    if isinstance(data, list) and data:
        return normalize_market(data[0])
    if key == "slug":
        # An /event/<slug> URL: return the event's first open market.
        ev = _get(GAMMA, "/events", {"slug": ref}, ttl=ttl)
        if isinstance(ev, list) and ev and isinstance(ev[0], dict):
            for raw in ev[0].get("markets") or []:
                m = normalize_market(raw, event=ev[0])
                if m and not m["closed"]:
                    return m
    return None


def order_book(token_id: str) -> dict:
    """Live book, normalised: bids best-first (high→low), asks best-first (low→high)."""
    data = _get(CLOB, "/book", {"token_id": token_id}, ttl=2)
    if not isinstance(data, dict):
        raise PolymarketError("unexpected order book response")

    def side(rows: Any) -> list[tuple[float, float]]:
        out = []
        for r in rows or []:
            if isinstance(r, dict):
                p, s = num(r.get("price")), num(r.get("size"))
                if p is not None and s and s > 0:
                    out.append((p, s))
        return out

    bids = sorted(side(data.get("bids")), key=lambda x: -x[0])
    asks = sorted(side(data.get("asks")), key=lambda x: x[0])
    return {"bids": bids, "asks": asks,
            "tick_size": num(data.get("tick_size")),
            "min_order_size": num(data.get("min_order_size"))}


def clear_cache() -> None:
    with _lock:
        _cache.clear()
