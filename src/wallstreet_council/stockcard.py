"""What the public page shows for one stock, in words a child can follow.

Only for stocks that serve the goal: today's picks, backup picks and the floor's watchlist. Never for
every stock in the market.

  chart      one year of daily closes and the market index, both as "% since a year ago"
  if_bought  what the budget invested a year ago would be worth today
  story      years on the market, best and worst calendar year, biggest fall, distance from its high
  news       recent headlines sorted by a free-tier model into good news and bad news, one plain line each
             about why it matters (cached for four hours)
"""
from __future__ import annotations

import json
import time

import pandas as pd
import yfinance as yf

from . import dossier, llm, market, memory

NEWS_MODELS = ["nvidia/nvidia/nemotron-3-super-120b-a12b", "gemini/gemini-3.1-flash-lite",
               "nvidia/nvidia/nemotron-3-ultra-550b-a55b"]


def _series(sym: str, period: str = "1y") -> pd.Series:
    s = yf.Ticker(sym).history(period=period, auto_adjust=True)["Close"].dropna()
    s.index = s.index.tz_localize(None) if s.index.tz is not None else s.index
    return s


def chart(sym: str, code: str) -> dict:
    s = _series(sym)
    b = _series(dossier.BENCHMARK.get(code, "^GSPC"))
    b = b.reindex(s.index, method="ffill").dropna()
    s = s.loc[b.index]
    if s.empty:
        return {}
    step = max(1, len(s) // 120)  # about 120 points is plenty for a phone
    idx = list(range(0, len(s), step)) + ([len(s) - 1] if (len(s) - 1) % step else [])
    return {"dates": [str(s.index[i].date()) for i in idx],
            "stock_pct": [round((float(s.iloc[i]) / float(s.iloc[0]) - 1) * 100, 1) for i in idx],
            "index_pct": [round((float(b.iloc[i]) / float(b.iloc[0]) - 1) * 100, 1) for i in idx],
            "index_name": "Nifty 50" if code == "IN" else "S&P 500",
            "year_change_pct": round((float(s.iloc[-1]) / float(s.iloc[0]) - 1) * 100, 1),
            "index_year_change_pct": round((float(b.iloc[-1]) / float(b.iloc[0]) - 1) * 100, 1)}


def news(sym: str, name: str, code: str) -> dict:
    key = f"news:{sym}"
    cached, _ = memory._get(key)
    if cached and time.time() - cached.get("ts", 0) < 4 * 3600:
        return cached
    q = f"{sym.split('.')[0]} {name} stock"
    heads = market.headlines(q, n=12, window="7d", edition="IN" if code == "IN" else "US")
    if not heads:
        return cached or {"good": [], "bad": [], "ts": time.time()}
    prompt = (f"COMPANY: {name} ({sym})\nHEADLINES (last 7 days):\n" + "\n".join(f"- {h['title']}" for h in heads)
              + "\n\nTASK: Sort the headlines that matter for this company's share price into good news (could push it "
                "up) and bad news (could push it down). Skip ones that are irrelevant or duplicates. For each, write "
                "one short line a 12-year-old understands, no jargon. At most 4 good and 4 bad. Only use what the "
                'headlines say.\nJSON: {"good": [{"headline": "", "plain": ""}], "bad": [{"headline": "", "plain": ""}]}')
    for model in NEWS_MODELS:
        try:
            d = llm.parse_json(llm.chat(model, "You explain stock news simply. JSON only.", prompt, max_tokens=1500,
                                        timeout=90)["text"])
        except llm.LLMError:
            continue
        if isinstance(d, dict):
            out = {"good": (d.get("good") or [])[:4], "bad": (d.get("bad") or [])[:4], "ts": time.time()}
            memory._put(key, out, 0)
            return out
    return cached or {"good": [], "bad": [], "ts": time.time()}


def story(sym: str, code: str) -> dict:
    d = dossier.build(sym, code)
    h = d.get("history", {})
    years = h.get("calendar_year_returns_pct") or {}
    best = max(years.items(), key=lambda kv: kv[1]) if years else None
    worst = min(years.items(), key=lambda kv: kv[1]) if years else None
    return {"years_listed": h.get("years_listed"), "first_date": h.get("first_date"),
            "best_year": {"year": best[0], "pct": best[1]} if best else None,
            "worst_year": {"year": worst[0], "pct": worst[1]} if worst else None,
            "biggest_fall_pct": h.get("max_drawdown_all_time_pct"), "off_high_pct": h.get("off_all_time_high_pct"),
            "cagr_5y_pct": h.get("cagr_5y_pct"), "next_earnings": d.get("next_earnings_date"),
            "recommendations": d.get("recommendations_now")}


def resolve(sym: str) -> str | None:
    """A watchlist entry like RELIANCE may need its exchange suffix."""
    for cand in (sym, f"{sym}.NS"):
        if not market.snapshot(cand).get("error"):
            return cand
    return None


def cached_build(sym: str, max_age: int = 7200) -> dict:
    cached, _ = memory._get(f"card:{sym}")
    if cached and time.time() - cached.get("built", 0) < max_age:
        return cached
    card = build(sym)
    if not card.get("error"):
        memory._put(f"card:{sym}", card, 0)
    return card


def build(sym: str, budget: float | None = None) -> dict:
    code = "IN" if sym.endswith((".NS", ".BO")) else "US"
    budget = budget or (1000.0 if code == "IN" else 10.0)
    snap = market.snapshot(sym)
    if snap.get("error"):
        return {"symbol": sym, "error": "no data"}
    f = market.fundamentals(sym)
    name = f.get("shortName") or sym.split(".")[0]
    ch = chart(sym, code)
    out = {"symbol": sym, "name": name, "market": code, "currency": "INR" if code == "IN" else "USD",
           "price": snap["price"], "today_pct": snap.get("chg_1d"), "chart": ch, "story": story(sym, code),
           "news": news(sym, name, code), "built": time.time()}
    if ch:
        out["if_bought_year_ago"] = {"amount": budget,
                                     "now": round(budget * (1 + ch["year_change_pct"] / 100), 2),
                                     "index_now": round(budget * (1 + ch["index_year_change_pct"] / 100), 2)}
    if code == "IN":
        out["whole_shares_for_budget"] = int(budget // snap["price"]) if snap["price"] else 0
    return out


def tickers_to_show(sessions: list[dict], limit: int = 8) -> list[str]:
    """Picks first, then backup picks, then the floor's watchlist."""
    out: list[str] = []
    for s in sessions:
        v = s.get("verdict") or {}
        if (s.get("mode") or "pick") == "pick":
            out += [t for t in (v.get("decision"), v.get("backup_pick")) if t and t not in ("CASH", "NONE")]
    floor = next((s for s in sessions if s["id"].startswith("floor-")), None)
    if floor:
        out += [w.get("ticker") for w in (memory.floor_digest(floor["id"]).get("watchlist") or [])
                if isinstance(w, dict) and w.get("ticker")]
    clean: list[str] = []
    for t in out:
        t = str(t).upper().strip()
        if not t or len(t) > 15 or " " in t or t in clean:
            continue
        r = resolve(t)
        if r and r not in clean:
            clean.append(r)
        if len(clean) >= limit:
            break
    return clean


def dump(obj) -> str:
    return json.dumps(obj, default=str)
