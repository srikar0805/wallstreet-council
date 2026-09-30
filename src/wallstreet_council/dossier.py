"""The full file on one stock: its whole price history and what the company has actually reported.

Built for the names seats pitch, so the cross-examination argues over decades of evidence rather than
one week of tape:
  history      years listed, CAGR over 1/3/5/10 years, max drawdown, calendar-year returns, distance from
               the all-time high, beta against the market's benchmark, how often this calendar month was up
  earnings     last quarters' revenue, net income and margins; EPS surprises and the next-day price reaction
  street       recommendation counts, recent upgrades and downgrades, price targets
  insiders     net insider buying or selling over six months; top institutions and their changes
  income       dividend yield over the last year, splits
Everything comes from Yahoo Finance and degrades field by field when a market (often NSE) lacks it.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

BENCHMARK = {"US": "^GSPC", "IN": "^NSEI"}


def _r(x, n=2):
    try:
        return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), n)
    except (TypeError, ValueError):
        return None


def _history_block(close: pd.Series, bench: pd.Series | None) -> dict:
    close = close.dropna()
    if close.empty:
        return {}
    last, now = float(close.iloc[-1]), close.index[-1]
    out: dict = {"first_date": str(close.index[0].date()), "years_listed": _r((now - close.index[0]).days / 365.25, 1)}
    for yrs in (1, 3, 5, 10):
        past = close[close.index <= now - pd.DateOffset(years=yrs)]
        if len(past):
            out[f"cagr_{yrs}y_pct"] = _r(((last / float(past.iloc[-1])) ** (1 / yrs) - 1) * 100, 1)
    peak = close.cummax()
    out["max_drawdown_all_time_pct"] = _r((close / peak - 1).min() * 100, 1)
    c5 = close[close.index >= now - pd.DateOffset(years=5)]
    if len(c5):
        out["max_drawdown_5y_pct"] = _r((c5 / c5.cummax() - 1).min() * 100, 1)
    out["off_all_time_high_pct"] = _r((last / float(close.max()) - 1) * 100, 1)
    yearly = close.groupby(close.index.year).last()
    yr = (yearly.pct_change() * 100).dropna().tail(8)
    out["calendar_year_returns_pct"] = {int(k): _r(v, 1) for k, v in yr.items()}
    monthly = close.resample("ME").last().pct_change().dropna()
    this_m = monthly[monthly.index.month == now.month]
    if len(this_m) >= 5:
        out["this_calendar_month"] = {"years": len(this_m), "up_pct_of_years": _r((this_m > 0).mean() * 100, 0),
                                      "median_return_pct": _r(this_m.median() * 100, 2),
                                      "caution": "seasonality is weak evidence"}
    d = close.pct_change().dropna().tail(504)
    out["up_days_2y_pct"] = _r((d > 0).mean() * 100, 0)
    out["worst_day_2y_pct"] = _r(d.min() * 100, 1)
    out["best_day_2y_pct"] = _r(d.max() * 100, 1)
    if bench is not None and len(bench):
        b = bench.pct_change().dropna()
        j = pd.concat([d, b], axis=1, join="inner").dropna()
        if len(j) > 60 and j.iloc[:, 1].var() > 0:
            out["beta_2y_vs_benchmark"] = _r(j.cov().iloc[0, 1] / j.iloc[:, 1].var(), 2)
            out["correlation_2y_vs_benchmark"] = _r(j.corr().iloc[0, 1], 2)
            bl = bench.dropna()
            past_b = bl[bl.index <= now - pd.DateOffset(years=1)]
            past_s = close[close.index <= now - pd.DateOffset(years=1)]
            if len(past_b) and len(past_s):
                out["excess_vs_benchmark_1y_pct"] = _r(
                    ((last / float(past_s.iloc[-1])) - (float(bl.iloc[-1]) / float(past_b.iloc[-1]))) * 100, 1)
    return out


def _earnings_block(t: yf.Ticker, close: pd.Series) -> dict:
    out: dict = {}
    try:
        q = t.quarterly_income_stmt
        rows = {"Total Revenue": "revenue", "Net Income": "net_income", "Operating Income": "operating_income"}
        qs = []
        for col in list(q.columns)[:5]:
            item = {"quarter": str(pd.Timestamp(col).date())}
            for k, name in rows.items():
                if k in q.index:
                    item[name] = _r(q.loc[k, col], 0)
            if item.get("revenue") and item.get("operating_income") is not None:
                item["operating_margin_pct"] = _r(item["operating_income"] / item["revenue"] * 100, 1)
            qs.append(item)
        if qs:
            out["quarters"] = qs
            if len(qs) == 5 and qs[0].get("revenue") and qs[4].get("revenue"):
                out["revenue_yoy_latest_q_pct"] = _r((qs[0]["revenue"] / qs[4]["revenue"] - 1) * 100, 1)
    except Exception:  # noqa: BLE001
        pass
    try:
        ed = t.earnings_dates
        past = ed[ed["Reported EPS"].notna()].head(6) if ed is not None and len(ed) else []
        reacts = []
        for ts, row in (past.iterrows() if len(past) else []):
            day = pd.Timestamp(ts).tz_localize(None).normalize() if pd.Timestamp(ts).tzinfo else pd.Timestamp(ts)
            c = close.copy()
            c.index = c.index.tz_localize(None) if c.index.tz is not None else c.index
            before, after = c[c.index < day], c[c.index > day]
            move = _r((float(after.iloc[0]) / float(before.iloc[-1]) - 1) * 100, 1) if len(before) and len(after) else None
            reacts.append({"date": str(day.date()), "eps": _r(row.get("Reported EPS")),
                           "estimate": _r(row.get("EPS Estimate")), "surprise_pct": _r(row.get("Surprise(%)"), 1),
                           "next_day_move_pct": move})
        if reacts:
            out["recent_earnings"] = reacts
        upcoming = ed[ed["Reported EPS"].isna()] if ed is not None and len(ed) else []
        if len(upcoming):
            out["next_earnings_date"] = str(pd.Timestamp(upcoming.index[-1]).date())
    except Exception:  # noqa: BLE001
        pass
    return out


def _street_block(t: yf.Ticker) -> dict:
    out: dict = {}
    try:
        rs = t.recommendations_summary
        if rs is not None and len(rs):
            out["recommendations_now"] = {k: int(rs.iloc[0][k]) for k in ("strongBuy", "buy", "hold", "sell",
                                                                            "strongSell") if k in rs.columns}
    except Exception:  # noqa: BLE001
        pass
    try:
        ud = t.upgrades_downgrades
        if ud is not None and len(ud):
            recent = ud[ud.index >= pd.Timestamp.now() - pd.Timedelta(days=90)].head(8)
            out["rating_changes_90d"] = [{"date": str(pd.Timestamp(i).date()), "firm": r.get("Firm"),
                                          "action": r.get("Action"), "to": r.get("ToGrade"),
                                          "target": _r(r.get("currentPriceTarget"))} for i, r in recent.iterrows()]
    except Exception:  # noqa: BLE001
        pass
    return out


def _owners_block(t: yf.Ticker) -> dict:
    out: dict = {}
    try:
        it = t.insider_transactions
        if it is not None and len(it):
            cutoff = pd.Timestamp.now() - pd.Timedelta(days=183)
            date_col = "Start Date" if "Start Date" in it.columns else None
            recent = it[pd.to_datetime(it[date_col]) >= cutoff] if date_col else it
            text = recent.get("Text", pd.Series(dtype=str)).fillna("").str.lower()
            val = pd.to_numeric(recent.get("Value"), errors="coerce").fillna(0)
            out["insider_6m"] = {"buys_value": _r(val[text.str.contains("purchase|buy")].sum(), 0),
                                 "sells_value": _r(val[text.str.contains("sale|sell")].sum(), 0),
                                 "transactions": int(len(recent))}
    except Exception:  # noqa: BLE001
        pass
    try:
        ih = t.institutional_holders
        if ih is not None and len(ih):
            out["top_institutions"] = [{"holder": r.get("Holder"), "pct_held": _r((r.get("pctHeld") or 0) * 100, 2),
                                        "pct_change": _r((r.get("pctChange") or 0) * 100, 1)}
                                       for _, r in ih.head(5).iterrows()]
    except Exception:  # noqa: BLE001
        pass
    return out


def build(symbol: str, market_code: str = "US") -> dict:
    t = yf.Ticker(symbol)
    try:
        h = t.history(period="max", auto_adjust=True)
    except Exception:  # noqa: BLE001
        h = pd.DataFrame()
    if h.empty:
        return {"symbol": symbol, "error": "no history"}
    close = h["Close"]
    try:
        bench = yf.Ticker(BENCHMARK.get(market_code, "^GSPC")).history(period="2y", auto_adjust=True)["Close"]
        idx = close.index.tz_localize(None) if close.index.tz is not None else close.index
        bench.index = bench.index.tz_localize(None) if bench.index.tz is not None else bench.index
        close_naive = close.copy()
        close_naive.index = idx
    except Exception:  # noqa: BLE001
        bench, close_naive = None, close
    out = {"symbol": symbol, "benchmark": BENCHMARK.get(market_code), "history": _history_block(close_naive, bench)}
    out.update(_earnings_block(t, close))
    out.update(_street_block(t))
    out.update(_owners_block(t))
    try:
        div = t.dividends
        if len(div):
            ttm = div[div.index >= div.index[-1] - timedelta(days=365)].sum()
            out["dividend_yield_ttm_pct"] = _r(ttm / float(close.iloc[-1]) * 100, 2)
        sp = t.splits
        if len(sp):
            out["splits"] = {str(k.date()): _r(v, 2) for k, v in sp.tail(4).items()}
    except Exception:  # noqa: BLE001
        pass
    out["built"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    return out
