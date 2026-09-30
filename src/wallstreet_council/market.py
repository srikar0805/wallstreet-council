"""The research desk: free data the council argues over.

Prices, history and screeners come from Yahoo Finance (yfinance). Headlines come from
Google News RSS. Sentiment is VADER over headlines, a crude first pass the models refine.
Nothing here needs a paid key.
"""
from __future__ import annotations

import calendar
import math
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import feedparser
import yfinance as yf
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

ET = ZoneInfo("America/New_York")
_vader = SentimentIntensityAnalyzer()

REGIME = {"SPY": "S&P 500", "QQQ": "Nasdaq 100", "IWM": "Russell 2000", "^VIX": "VIX",
          "^TNX": "10y yield", "DX-Y.NYB": "Dollar index", "CL=F": "Crude oil", "GC=F": "Gold",
          "BTC-USD": "Bitcoin"}
SECTORS = {"XLK": "Tech", "XLF": "Financials", "XLE": "Energy", "XLV": "Health", "XLY": "Discretionary",
           "XLP": "Staples", "XLI": "Industrials", "XLU": "Utilities", "XLC": "Communications",
           "XLB": "Materials", "XLRE": "Real estate", "SMH": "Semis"}
CORE = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "AVGO", "JPM", "LLY", "NFLX",
        "COST", "PLTR", "UBER", "DKNG", "DIS", "NKE"]
MACRO_QUERIES = ["stock market today", "Federal Reserve interest rates", "inflation CPI jobs report",
                 "tariffs trade policy stocks", "earnings this week", "geopolitics oil markets"]
SPORTS_QUERIES = ["sports business stocks sponsorship", "sports betting stocks DraftKings FanDuel",
                  "NFL NBA media rights deal", "major sporting event consumer spending"]


def headlines(query: str, n: int = 6, window: str = "2d") -> list[dict]:
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(f"{query} when:{window}")
           + "&hl=en-US&gl=US&ceid=US:en")
    try:
        feed = feedparser.parse(url)
    except Exception:
        return []
    out = []
    for e in feed.entries[:n]:
        title = e.get("title", "")
        out.append({"title": title, "published": e.get("published", ""),
                    "sentiment": round(_vader.polarity_scores(title)["compound"], 3)})
    return out


def _pct(a: float, b: float) -> float | None:
    return round((a / b - 1) * 100, 2) if a and b else None


def _rsi(closes, n: int = 14) -> float | None:
    if len(closes) <= n:
        return None
    d = closes.diff().dropna()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean().iloc[-1]
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean().iloc[-1]
    return round(100.0, 1) if dn == 0 else round(100 - 100 / (1 + up / dn), 1)


def snapshot(symbol: str) -> dict:
    """Price history and technicals for one symbol."""
    h = yf.Ticker(symbol).history(period="1y", auto_adjust=True)
    if h.empty:
        return {"symbol": symbol, "error": "no history"}
    c, v = h["Close"], h["Volume"]
    last = float(c.iloc[-1])
    rets = c.pct_change().dropna()
    vol20 = float(rets.tail(20).std() * math.sqrt(252) * 100) if len(rets) > 20 else None
    at = lambda k: float(c.iloc[-1 - k]) if len(c) > k else None  # noqa: E731
    sma = lambda k: float(c.tail(k).mean()) if len(c) >= k else None  # noqa: E731
    hi, lo = float(c.max()), float(c.min())
    return {
        "symbol": symbol, "price": round(last, 2), "as_of": str(h.index[-1].date()),
        "chg_1d": _pct(last, at(1)), "chg_5d": _pct(last, at(5)), "chg_1m": _pct(last, at(21)),
        "chg_3m": _pct(last, at(63)), "chg_1y": _pct(last, float(c.iloc[0])),
        "rsi14": _rsi(c), "vs_sma20": _pct(last, sma(20)), "vs_sma50": _pct(last, sma(50)),
        "vs_sma200": _pct(last, sma(200)), "vol_annual_pct": round(vol20, 1) if vol20 else None,
        "volume_vs_20d": round(float(v.iloc[-1] / v.tail(20).mean()), 2) if v.tail(20).mean() else None,
        "range_52w_pos": round((last - lo) / (hi - lo), 2) if hi > lo else None,
    }


def fundamentals(symbol: str) -> dict:
    t = yf.Ticker(symbol)
    try:
        i = t.info or {}
    except Exception:
        i = {}
    keep = ["shortName", "sector", "industry", "marketCap", "trailingPE", "forwardPE", "pegRatio",
            "revenueGrowth", "earningsGrowth", "profitMargins", "debtToEquity", "beta",
            "targetMeanPrice", "recommendationKey", "numberOfAnalystOpinions", "shortPercentOfFloat"]
    out = {k: i.get(k) for k in keep if i.get(k) is not None}
    try:
        cal = t.calendar or {}
        ed = cal.get("Earnings Date")
        if ed:
            out["next_earnings"] = str(ed[0] if isinstance(ed, list) else ed)
    except Exception:
        pass
    return out


def screened(limit: int = 6) -> list[str]:
    """Today's movers, liquid names only (market cap at least $2B, price at least $5)."""
    syms: list[str] = []
    for key in ("most_actives", "day_gainers", "day_losers"):
        try:
            quotes = yf.screen(key, count=25)["quotes"]
        except Exception:
            continue
        ok = [q["symbol"] for q in quotes
              if (q.get("marketCap") or 0) >= 2e9 and (q.get("regularMarketPrice") or 0) >= 5]
        syms += ok[:limit]
    return list(dict.fromkeys(syms))


def trading_days_left(today: date | None = None) -> int:
    today = today or datetime.now(ET).date()
    last = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
    return sum(1 for k in range((last - today).days + 1) if (today + timedelta(k)).weekday() < 5)


def horizon_end(horizon: str, today: date | None = None) -> date:
    """End date for a plain-English horizon: 'end of this month', 'end of October 2026', 'end of next month'."""
    today = today or datetime.now(ET).date()
    h = horizon.lower()
    y, m = today.year, today.month
    if "next month" in h:
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    else:
        for i, name in enumerate(calendar.month_name[1:], 1):
            if name.lower() in h or f" {name[:3].lower()} " in f" {h} ":
                m = i
                y = next((int(t) for t in h.split() if t.isdigit() and len(t) == 4), y + (1 if i < today.month else 0))
                break
    return date(y, m, calendar.monthrange(y, m)[1])


def trading_days_between(start: date, end: date) -> int:
    return sum(1 for k in range((end - start).days + 1) if (start + timedelta(k)).weekday() < 5)


def market_clock() -> dict:
    now = datetime.now(ET)
    open_t, close_t = dtime(9, 30), dtime(16, 0)
    is_open = now.weekday() < 5 and open_t <= now.time() < close_t
    nxt = now
    if now.weekday() >= 5 or now.time() >= close_t:
        nxt = now + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
    next_open = datetime.combine(nxt.date(), open_t, ET) if not is_open else None
    last_day = date(now.year, now.month, calendar.monthrange(now.year, now.month)[1])
    return {"now_et": now.strftime("%Y-%m-%d %H:%M %Z"), "market_open": is_open,
            "next_open_et": next_open.strftime("%Y-%m-%d %H:%M") if next_open else None,
            "month_end": str(last_day), "trading_days_left_incl_today": trading_days_left(now.date()),
            "note": "US holidays are not modelled"}


def build_brief(extra: list[str] | None = None, progress=None) -> dict:
    """Everything the council sees. `progress(msg)` is called as each part lands."""
    say = progress or (lambda m: None)
    say("Reading the tape: indices, VIX, yields, dollar, oil, gold, bitcoin")
    with ThreadPoolExecutor(12) as ex:
        regime = dict(zip(REGIME.values(), ex.map(snapshot, REGIME)))
        sectors = dict(zip(SECTORS.values(), ex.map(snapshot, SECTORS)))
    regime = {k: {f: v.get(f) for f in ("price", "chg_1d", "chg_5d", "chg_1m", "rsi14")} for k, v in regime.items()}
    sectors = {k: {f: v.get(f) for f in ("chg_1d", "chg_5d", "chg_1m")} for k, v in sectors.items()}

    say("Screening today's most active, top gaining and top losing large caps")
    universe = list(dict.fromkeys([s.upper() for s in (extra or [])] + screened() + CORE))[:30]

    say(f"Pulling one year of history, fundamentals and headlines for {len(universe)} tickers")

    def one(sym: str) -> dict:
        d = snapshot(sym)
        if d.get("error"):
            return d
        d["fundamentals"] = fundamentals(sym)
        name = d["fundamentals"].get("shortName", sym)
        news = headlines(f"{sym} {name} stock", n=5)
        d["news"] = news
        d["news_sentiment_avg"] = round(sum(n["sentiment"] for n in news) / len(news), 3) if news else None
        return d

    with ThreadPoolExecutor(10) as ex:
        cands = [d for d in ex.map(one, universe) if not d.get("error")]

    say("Scanning macro, policy, geopolitics and sports-business headlines")
    with ThreadPoolExecutor(10) as ex:
        macro = dict(zip(MACRO_QUERIES, ex.map(lambda q: headlines(q, 5), MACRO_QUERIES)))
        sports = dict(zip(SPORTS_QUERIES, ex.map(lambda q: headlines(q, 4), SPORTS_QUERIES)))

    return {"clock": market_clock(), "regime": regime, "sectors": sectors,
            "macro_news": macro, "sports_news": sports, "candidates": cands}


def compact(brief: dict, only: set[str] | None = None, news: bool = True) -> str:
    """A token-lean text version of the brief for prompts. `only` keeps just those candidates, and
    news=False drops the macro and sports headline blocks (the chair's slim brief)."""
    import json
    lines = ["CLOCK " + json.dumps(brief["clock"]),
             "REGIME " + json.dumps(brief["regime"]),
             "SECTORS " + json.dumps(brief["sectors"])]
    if news:
        lines.append("MACRO HEADLINES:")
        for q, hs in brief["macro_news"].items():
            lines += [f"  [{q}] ({h['sentiment']:+.2f}) {h['title']}" for h in hs]
        lines.append("SPORTS / CULTURE HEADLINES:")
        for q, hs in brief["sports_news"].items():
            lines += [f"  [{q}] ({h['sentiment']:+.2f}) {h['title']}" for h in hs]
    lines.append("CANDIDATES:")
    for c in brief["candidates"]:
        if only is not None and c["symbol"] not in only:
            continue
        f = c.get("fundamentals", {})
        tech = {k: c.get(k) for k in ("price", "chg_1d", "chg_5d", "chg_1m", "chg_3m", "chg_1y", "rsi14",
                                      "vs_sma20", "vs_sma50", "vs_sma200", "vol_annual_pct", "volume_vs_20d",
                                      "range_52w_pos")}
        lines.append(f"- {c['symbol']} {json.dumps(tech)} FUND {json.dumps(f, default=str)} "
                     f"NEWS_SENT {c.get('news_sentiment_avg')}")
        lines += [f"    ({h['sentiment']:+.2f}) {h['title']}" for h in c.get("news", [])[:4]]
    return "\n".join(lines)


def price_now(symbol: str) -> float:
    return float(yf.Ticker(symbol).fast_info["lastPrice"])
