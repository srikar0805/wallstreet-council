"""The research desk: free data the council argues over, for the US and Indian markets.

Prices, history and screeners come from Yahoo Finance (yfinance; NSE tickers end in .NS).
Headlines come from Google News RSS in the market's own edition. Sentiment is VADER over
headlines, a crude first pass the models refine. Nothing here needs a paid key.
"""
from __future__ import annotations

import calendar
import json
import math
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import feedparser
import yfinance as yf
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

ET = ZoneInfo("America/New_York")
IST = ZoneInfo("Asia/Kolkata")
_vader = SentimentIntensityAnalyzer()

MARKETS: dict[str, dict] = {
    "US": {
        "name": "US equities (NYSE, Nasdaq)", "tz": ET, "tz_label": "US Eastern", "open": dtime(9, 30),
        "close": dtime(16, 0), "currency": "USD", "sym": "$", "fractional": True, "news_edition": "US",
        "regime": {"SPY": "S&P 500", "QQQ": "Nasdaq 100", "IWM": "Russell 2000", "^VIX": "VIX",
                   "^TNX": "10y yield", "DX-Y.NYB": "Dollar index", "CL=F": "Crude oil", "GC=F": "Gold",
                   "BTC-USD": "Bitcoin"},
        "sectors": {"XLK": "Tech", "XLF": "Financials", "XLE": "Energy", "XLV": "Health", "XLY": "Discretionary",
                    "XLP": "Staples", "XLI": "Industrials", "XLU": "Utilities", "XLC": "Communications",
                    "XLB": "Materials", "XLRE": "Real estate", "SMH": "Semis"},
        "core": ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "AVGO", "JPM", "LLY", "NFLX",
                 "COST", "PLTR", "UBER", "DKNG", "DIS", "NKE"],
        "macro": ["stock market today", "Federal Reserve interest rates", "inflation CPI jobs report",
                  "tariffs trade policy stocks", "earnings this week", "geopolitics oil markets"],
        "sports": ["sports business stocks sponsorship", "sports betting stocks DraftKings FanDuel",
                   "NFL NBA media rights deal", "major sporting event consumer spending"],
        "min_cap": 2e9, "min_price": 5,
    },
    "IN": {
        "name": "Indian equities (NSE, BSE)", "tz": IST, "tz_label": "IST", "open": dtime(9, 15),
        "close": dtime(15, 30), "currency": "INR", "sym": "₹", "fractional": False, "news_edition": "IN",
        "regime": {"^NSEI": "Nifty 50", "^BSESN": "Sensex", "^NSEBANK": "Bank Nifty", "^INDIAVIX": "India VIX",
                   "USDINR=X": "USD/INR", "BZ=F": "Brent crude", "GC=F": "Gold", "^GSPC": "S&P 500 (overnight)"},
        "sectors": {"^CNXIT": "IT", "^CNXPHARMA": "Pharma", "^CNXAUTO": "Auto", "^CNXFMCG": "FMCG",
                    "^CNXMETAL": "Metal", "^CNXENERGY": "Energy", "^CNXREALTY": "Realty", "^CNXPSUBANK": "PSU banks",
                    "^CNXINFRA": "Infra", "^CNXMEDIA": "Media"},
        "core": ["RELIANCE.NS", "HDFCBANK.NS", "ICICIBANK.NS", "TCS.NS", "INFY.NS", "BHARTIARTL.NS", "SBIN.NS",
                 "ITC.NS", "LT.NS", "HINDUNILVR.NS", "BAJFINANCE.NS", "MARUTI.NS", "M&M.NS", "SUNPHARMA.NS",
                 "TITAN.NS", "ETERNAL.NS", "ADANIENT.NS", "TMPV.NS"],
        "macro": ["Sensex Nifty today", "RBI repo rate policy", "FII DII flows India stocks", "SEBI rules",
                  "India GDP inflation CPI", "rupee dollar crude India markets"],
        "sports": ["IPL BCCI media rights sponsorship", "cricket sponsorship brand stocks India",
                   "India festive season consumer demand stocks", "Bollywood box office media stocks"],
        "min_cap": 1.5e11, "min_price": 20,  # market cap in rupees: about 1,500 crore
    },
}


def mkt(code: str | None) -> dict:
    return MARKETS[(code or "US").upper()]


def money(amount: float, code: str | None) -> str:
    return f"{mkt(code)['sym']}{amount:,.2f}"


def headlines(query: str, n: int = 6, window: str = "2d", edition: str = "US") -> list[dict]:
    loc = {"US": "&hl=en-US&gl=US&ceid=US:en", "IN": "&hl=en-IN&gl=IN&ceid=IN:en"}[edition]
    url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(f"{query} when:{window}") + loc
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
    try:
        h = yf.Ticker(symbol).history(period="1y", auto_adjust=True)
    except Exception:  # noqa: BLE001
        h = None
    if h is None or h.empty:
        return {"symbol": symbol, "error": "no history"}
    c, v = h["Close"], h["Volume"]
    last = float(c.iloc[-1])
    rets = c.pct_change().dropna()
    vol20 = float(rets.tail(20).std() * math.sqrt(252) * 100) if len(rets) > 20 else None
    at = lambda k: float(c.iloc[-1 - k]) if len(c) > k else None  # noqa: E731
    sma = lambda k: float(c.tail(k).mean()) if len(c) >= k else None  # noqa: E731
    hi, lo = float(c.max()), float(c.min())
    vavg = float(v.tail(20).mean()) if len(v) else 0
    return {
        "symbol": symbol, "price": round(last, 2), "as_of": str(h.index[-1].date()), "days_of_history": len(c),
        "chg_1d": _pct(last, at(1)), "chg_5d": _pct(last, at(5)), "chg_1m": _pct(last, at(21)),
        "chg_3m": _pct(last, at(63)), "chg_1y": _pct(last, float(c.iloc[0])),
        "rsi14": _rsi(c), "vs_sma20": _pct(last, sma(20)), "vs_sma50": _pct(last, sma(50)),
        "vs_sma200": _pct(last, sma(200)), "vol_annual_pct": round(vol20, 1) if vol20 else None,
        "volume_vs_20d": round(float(v.iloc[-1]) / vavg, 2) if vavg else None,
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
            "targetMeanPrice", "recommendationKey", "numberOfAnalystOpinions", "shortPercentOfFloat",
            "heldPercentInsiders", "heldPercentInstitutions", "dividendYield"]
    out = {k: i.get(k) for k in keep if i.get(k) is not None}
    try:
        cal = t.calendar or {}
        ed = cal.get("Earnings Date")
        if ed:
            out["next_earnings"] = str(ed[0] if isinstance(ed, list) else ed)
    except Exception:
        pass
    return out


def screened(code: str = "US", limit: int = 6) -> list[str]:
    """Today's movers, liquid names only."""
    m = mkt(code)
    syms: list[str] = []
    if code == "US":
        for key in ("most_actives", "day_gainers", "day_losers"):
            try:
                quotes = yf.screen(key, count=25)["quotes"]
            except Exception:
                continue
            syms += [q["symbol"] for q in quotes if (q.get("marketCap") or 0) >= m["min_cap"]
                     and (q.get("regularMarketPrice") or 0) >= m["min_price"]][:limit]
    else:
        from yfinance import EquityQuery as Q
        base = [Q("eq", ["region", "in"]), Q("eq", ["exchange", "NSI"]), Q("gt", ["intradaymarketcap", m["min_cap"]])]
        for field, asc in (("percentchange", False), ("percentchange", True), ("dayvolume", False)):
            try:
                quotes = yf.screen(Q("and", base), sortField=field, sortAsc=asc, size=25)["quotes"]
            except Exception:
                continue
            syms += [q["symbol"] for q in quotes if (q.get("regularMarketPrice") or 0) >= m["min_price"]][:limit]
    return list(dict.fromkeys(syms))


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


def market_clock(code: str = "US") -> dict:
    m = mkt(code)
    tz, open_t, close_t = m["tz"], m["open"], m["close"]
    now = datetime.now(tz)
    is_open = now.weekday() < 5 and open_t <= now.time() < close_t
    nxt = now
    if now.weekday() >= 5 or now.time() >= close_t:
        nxt = now + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
    next_open = datetime.combine(nxt.date(), open_t, tz) if not is_open else None
    last_day = date(now.year, now.month, calendar.monthrange(now.year, now.month)[1])
    return {"market": code, "now_local": now.strftime("%Y-%m-%d %H:%M %Z"), "market_open": is_open,
            "hours": f"{open_t:%H:%M}-{close_t:%H:%M} {m['tz_label']}",
            "next_open": next_open.strftime("%Y-%m-%d %H:%M") if next_open else None,
            "month_end": str(last_day), "trading_days_left_incl_today": trading_days_between(now.date(), last_day),
            "note": "exchange holidays are not modelled"}


def regime_and_sectors(code: str) -> tuple[dict, dict]:
    m = mkt(code)
    with ThreadPoolExecutor(12) as ex:
        regime = dict(zip(m["regime"].values(), ex.map(snapshot, m["regime"])))
        sectors = dict(zip(m["sectors"].values(), ex.map(snapshot, m["sectors"])))
    regime = {k: {f: v.get(f) for f in ("price", "chg_1d", "chg_5d", "chg_1m", "rsi14")} for k, v in regime.items()}
    sectors = {k: {f: v.get(f) for f in ("chg_1d", "chg_5d", "chg_1m")} for k, v in sectors.items()}
    return regime, sectors


def candidate(sym: str, edition: str = "US") -> dict:
    d = snapshot(sym)
    if d.get("error"):
        return d
    d["fundamentals"] = fundamentals(sym)
    name = d["fundamentals"].get("shortName", sym)
    news = headlines(f"{sym.split('.')[0]} {name} stock", n=5, edition=edition)
    d["news"] = news
    d["news_sentiment_avg"] = round(sum(n["sentiment"] for n in news) / len(news), 3) if news else None
    return d


def build_brief(extra: list[str] | None = None, progress=None, code: str = "US", budget: float | None = None) -> dict:
    """Everything the council sees for a stock pick. `progress(msg)` is called as each part lands."""
    m = mkt(code)
    say = progress or (lambda msg: None)
    say(f"Reading the tape for {m['name']}: " + ", ".join(m["regime"].values()))
    regime, sectors = regime_and_sectors(code)

    say("Screening today's most active, top gaining and top losing large caps")
    universe = list(dict.fromkeys([s.upper() for s in (extra or [])] + screened(code) + m["core"]))[:30]
    say(f"Pulling one year of history, fundamentals and headlines for {len(universe)} tickers")
    with ThreadPoolExecutor(10) as ex:
        cands = [d for d in ex.map(lambda s: candidate(s, m["news_edition"]), universe) if not d.get("error")]
    if not m["fractional"] and budget:
        for c in cands:  # NSE and BSE trade whole shares only
            c["whole_shares_affordable"] = int(budget // c["price"]) if c.get("price") else 0

    say("Scanning macro, policy, geopolitics and sports-business headlines")
    with ThreadPoolExecutor(10) as ex:
        macro = dict(zip(m["macro"], ex.map(lambda q: headlines(q, 5, edition=m["news_edition"]), m["macro"])))
        sports = dict(zip(m["sports"], ex.map(lambda q: headlines(q, 4, edition=m["news_edition"]), m["sports"])))

    return {"clock": market_clock(code), "regime": regime, "sectors": sectors,
            "macro_news": macro, "sports_news": sports, "candidates": cands}


def compact(brief: dict, only: set[str] | None = None, news: bool = True) -> str:
    """A token-lean text version of the brief for prompts. `only` keeps just those candidates, and
    news=False drops the headline blocks (the chair's slim brief). Extra top-level blocks (IPOs,
    topic research) are included as JSON."""
    lines = ["CLOCK " + json.dumps(brief["clock"])]
    if brief.get("regime"):
        lines.append("REGIME " + json.dumps(brief["regime"]))
    if brief.get("sectors"):
        lines.append("SECTORS " + json.dumps(brief["sectors"]))
    for key in ("costs", "ipos", "dossiers"):
        if brief.get(key):
            lines.append(key.upper() + " " + json.dumps(brief[key], default=str))
    if news:
        for block, label in (("macro_news", "MACRO HEADLINES"), ("sports_news", "SPORTS / CULTURE HEADLINES"),
                             ("ipo_news", "IPO HEADLINES"), ("topic_news", "TOPIC HEADLINES")):
            if brief.get(block):
                lines.append(label + ":")
                for q, hs in brief[block].items():
                    lines += [f"  [{q}] ({h['sentiment']:+.2f}) {h['title']}" for h in hs]
    if brief.get("candidates"):
        lines.append("CANDIDATES:")
    for c in brief.get("candidates", []):
        if only is not None and c["symbol"] not in only:
            continue
        f = c.get("fundamentals", {})
        tech = {k: c.get(k) for k in ("price", "chg_1d", "chg_5d", "chg_1m", "chg_3m", "chg_1y", "rsi14",
                                      "vs_sma20", "vs_sma50", "vs_sma200", "vol_annual_pct", "volume_vs_20d",
                                      "range_52w_pos", "whole_shares_affordable") if c.get(k) is not None}
        lines.append(f"- {c['symbol']} {json.dumps(tech)} FUND {json.dumps(f, default=str)} "
                     f"NEWS_SENT {c.get('news_sentiment_avg')}")
        lines += [f"    ({h['sentiment']:+.2f}) {h['title']}" for h in c.get("news", [])[:4]]
    return "\n".join(lines)


def price_now(symbol: str) -> float:
    return float(yf.Ticker(symbol).fast_info["lastPrice"])
