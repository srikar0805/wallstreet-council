"""The IPO desk: what is open, coming, or just listed, in India and the US.

India  NSE's public JSON (current issues with live subscription multiples, upcoming issues, past
       issues). Listing performance of recent issues is computed from Yahoo prices against the top
       of the price band.
US     Nasdaq's IPO calendar API (upcoming, priced this month, recently filed).
Both   Google News headlines on subscription, grey market premium (GMP, unofficial) and listings.

NSE sometimes blocks clients without its cookies, so every call degrades to "unavailable" rather
than failing the council.
"""
from __future__ import annotations

import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from http.cookiejar import CookieJar

from . import market

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/128.0 Safari/537.36", "Accept": "application/json, text/plain, */*",
      "Accept-Language": "en-US,en;q=0.9"}


def _get_json(url: str, opener=None, referer: str | None = None):
    req = urllib.request.Request(url, headers={**UA, **({"Referer": referer} if referer else {})})
    with (opener or urllib.request.build_opener()).open(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _nse_opener():
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    try:  # the home page sets the cookies the API sometimes wants
        op.open(urllib.request.Request("https://www.nseindia.com/", headers=UA), timeout=15).read(2048)
    except Exception:  # noqa: BLE001
        pass
    return op


def _band_top(price: str) -> float | None:
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", (price or "").replace(",", ""))]
    return max(nums) if nums else None


def india() -> dict:
    op = _nse_opener()
    out: dict = {"source": "NSE"}
    try:
        cur = _get_json("https://www.nseindia.com/api/ipo-current-issue", op, "https://www.nseindia.com/")
        out["open_now"] = [{"company": x.get("companyName"), "symbol": x.get("symbol"),
                            "series": x.get("series"),  # EQ = mainboard, SME = SME platform
                            "price_band": x.get("issuePrice"), "opens": x.get("issueStartDate"),
                            "closes": x.get("issueEndDate"), "shares_offered": x.get("noOfSharesOffered"),
                            "subscribed_times": round(float(x["noOfTime"]), 2) if x.get("noOfTime") else None}
                           for x in cur]
    except Exception as e:  # noqa: BLE001
        out["open_now_error"] = str(e)[:120]
    try:
        up = _get_json("https://www.nseindia.com/api/all-upcoming-issues?category=ipo", op, "https://www.nseindia.com/")
        opened = {x["symbol"] for x in out.get("open_now", [])}
        out["upcoming"] = [{"company": x.get("companyName"), "symbol": x.get("symbol"), "series": x.get("series"),
                            "price_band": x.get("issuePrice"), "opens": x.get("issueStartDate"),
                            "closes": x.get("issueEndDate")} for x in up if x.get("symbol") not in opened]
    except Exception as e:  # noqa: BLE001
        out["upcoming_error"] = str(e)[:120]
    try:
        past = _get_json("https://www.nseindia.com/api/public-past-issues", op, "https://www.nseindia.com/")
        cutoff = datetime.now() - timedelta(days=45)
        recent = []
        for x in past[:80]:
            try:
                listed = datetime.strptime(x.get("listingDate", ""), "%d-%b-%Y")
            except ValueError:
                continue
            if listed >= cutoff and x.get("securityType") == "EQ":
                recent.append({"company": x.get("company"), "symbol": x.get("symbol"), "listed": str(listed.date()),
                               "issue_price": _band_top(x.get("issuePrice")) or _band_top(x.get("priceRange"))})

        def perf(r):
            s = market.snapshot(r["symbol"] + ".NS")
            if s.get("price") and r["issue_price"]:
                r["price_now"] = s["price"]
                r["return_since_issue_pct"] = round((s["price"] / r["issue_price"] - 1) * 100, 1)
            return r
        with ThreadPoolExecutor(8) as ex:
            out["recently_listed"] = list(ex.map(perf, recent[:12]))
    except Exception as e:  # noqa: BLE001
        out["recently_listed_error"] = str(e)[:120]
    return out


def us() -> dict:
    out: dict = {"source": "Nasdaq IPO calendar"}
    try:
        d = _get_json(f"https://api.nasdaq.com/api/ipo/calendar?date={datetime.now(market.ET):%Y-%m}")["data"]
        pick = lambda rows, keys: [{k: r.get(k) for k in keys} for r in (rows or [])]  # noqa: E731
        out["upcoming"] = pick((d.get("upcoming") or {}).get("upcomingTable", {}).get("rows"),
                               ["proposedTickerSymbol", "companyName", "proposedExchange", "proposedSharePrice",
                                "sharesOffered", "expectedPriceDate", "dollarValueOfSharesOffered"])
        priced = pick((d.get("priced") or {}).get("rows"),
                      ["proposedTickerSymbol", "companyName", "proposedExchange", "proposedSharePrice",
                       "pricedDate", "dollarValueOfSharesOffered"])

        def perf(r):
            s = market.snapshot(r["proposedTickerSymbol"]) if r.get("proposedTickerSymbol") else {}
            ipo_px = _band_top(r.get("proposedSharePrice") or "")
            if s.get("price") and ipo_px:
                r["price_now"] = s["price"]
                r["return_since_ipo_pct"] = round((s["price"] / ipo_px - 1) * 100, 1)
            return r
        with ThreadPoolExecutor(8) as ex:
            out["priced_this_month"] = list(ex.map(perf, priced[:12]))
        out["recently_filed"] = pick((d.get("filed") or {}).get("rows"),
                                     ["proposedTickerSymbol", "companyName", "filedDate",
                                      "dollarValueOfSharesOffered"])[:12]
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)[:160]
    return out


NEWS = {
    "IN": ["IPO subscription status today", "IPO GMP grey market premium today", "IPO listing gains NSE BSE",
           "SEBI IPO approval DRHP", "upcoming IPO next week India"],
    "US": ["IPO this week", "IPO priced above range", "IPO first day trading debut", "IPO filing S-1"],
}

RULES = {
    "IN": "Indian IPO facts: retail applicants bid for whole lots at the upper band; oversubscribed retail books "
          "are allotted by lottery, so most applicants get nothing or one lot; GMP is an unofficial grey-market "
          "quote and often wrong; SME issues (series SME) are far riskier and less liquid than mainboard (EQ). "
          "Subscription multiples change daily until close; the last day usually dominates. A mainboard retail "
          "application is at least one lot, usually worth about Rs 14,000-15,000; SME minimums are far higher. "
          "When the retail book is oversubscribed N times, a one-lot applicant's chance of allotment is roughly 1/N. "
          "Listing-day gains are taxed as short-term capital gains.",
    "US": "US IPO facts: retail investors rarely get shares at the offer price; most can only buy at the open on "
          "listing day, often well above the offer; lock-up expiries (usually 180 days) can pressure prices. "
          "Directed-share or broker IPO-access programs (for example on some app brokers) allot small amounts at "
          "the offer price, often with penalties for flipping.",
}


def build(codes: list[str], progress=None) -> tuple[dict, dict]:
    say = progress or (lambda m: None)
    ipos, news = {}, {}
    for code in codes:
        say(f"Pulling the {code} IPO calendar" + (" from NSE (live subscription figures)" if code == "IN"
                                                   else " from Nasdaq"))
        ipos[code] = india() if code == "IN" else us()
        ipos[code]["facts"] = RULES[code]
        edition = "IN" if code == "IN" else "US"
        with ThreadPoolExecutor(6) as ex:
            news.update(dict(zip([f"{code}: {q}" for q in NEWS[code]],
                                 ex.map(lambda q: market.headlines(q, 5, "3d", edition), NEWS[code]))))
    return ipos, news
