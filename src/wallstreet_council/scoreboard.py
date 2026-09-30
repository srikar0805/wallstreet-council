"""Paper portfolio marks and the seat leaderboard: who on the council is actually right."""
from __future__ import annotations

from functools import lru_cache

from . import market, store


@lru_cache(maxsize=256)
def _px(symbol: str, _bucket: int) -> float | None:
    try:
        return market.price_now(symbol)
    except Exception:  # noqa: BLE001
        return None


def price(symbol: str) -> float | None:
    import time
    return _px(symbol, int(time.time() // 60))  # one fetch per symbol per minute


def portfolio() -> dict:
    rows = []
    for p in store.positions():
        now = p["exit_price"] if p["status"] == "closed" else price(p["ticker"])
        value = p["shares"] * now if now else None
        rows.append({**p, "price_now": round(now, 4) if now else None,
                     "value_now": round(value, 2) if value else None,
                     "pnl": round(value - p["dollars"], 2) if value else None,
                     "pnl_pct": round((value / p["dollars"] - 1) * 100, 2) if value else None})
    totals: dict[str, dict] = {}
    for r in rows:
        t = totals.setdefault(r.get("currency") or "USD", {"invested": 0.0, "value": 0.0})
        t["invested"] += r["dollars"]
        t["value"] += r["value_now"] or r["dollars"]
    for t in totals.values():
        t.update({k: round(v, 2) for k, v in t.items()})
        t["pnl"] = round(t["value"] - t["invested"], 2)
    return {"positions": rows, "totals_by_currency": totals, "note": "Paper trading only."}


def leaderboard() -> list[dict]:
    """Every final vote, marked to market. CASH votes score 0%."""
    stats: dict[str, dict] = {}
    for e in store.events():
        if e["kind"] != "vote" or not e["data"]:
            continue
        d = e["data"]
        s = stats.setdefault(e["speaker"], {"seat": e["speaker"], "model": e["model"], "votes": 0, "returns": []})
        s["votes"] += 1
        if d.get("vote") == "CASH" or not d.get("price_at_vote"):
            s["returns"].append(0.0)
            continue
        now = price(d["vote"])
        if now:
            s["returns"].append((now / d["price_at_vote"] - 1) * 100)
    out = []
    for s in stats.values():
        r = s.pop("returns")
        s["avg_return_pct"] = round(sum(r) / len(r), 2) if r else None
        s["hit_rate_pct"] = round(100 * sum(1 for x in r if x > 0) / len(r), 1) if r else None
        out.append(s)
    return sorted(out, key=lambda s: -(s["avg_return_pct"] or -1e9))
