"""What trading actually costs, what the taxman takes, and what a 10x goal really demands.

The rates below are the published schedules as of mid-2026 to the best of the author's knowledge.
They change (the SEC resets its fee every fiscal year, brokers reprice, budgets change tax rates),
so every figure carries `verify` links and the council is told to treat them as approximate.
Override any of them with ~/.wallstreet-council/costs.json (same shape as PROFILES / TAX).

Tax figures are general rules, not advice for a specific person: residency status (for example an
Indian student on F-1 or OPT in the US, who may be an NRI in India and a nonresident alien in the US)
changes what applies. The council must say so instead of guessing.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

OVERRIDE = Path(os.environ.get("COUNCIL_COSTS", Path.home() / ".wallstreet-council" / "costs.json"))

PROFILES: dict[str, dict] = {
    "IN-delivery-zero-brokerage": {
        "market": "IN", "label": "India, delivery (buy today, sell another day), zero-brokerage broker e.g. Zerodha",
        "brokerage_pct": 0.0, "brokerage_cap": 0.0, "stt_buy_pct": 0.1, "stt_sell_pct": 0.1,
        "exchange_txn_pct": 0.00297, "sebi_pct": 0.0001, "stamp_buy_pct": 0.015, "gst_pct": 18.0,
        "dp_charge_per_sell": 15.93,
        "notes": "DP (depository) charge is a flat fee per stock per day you sell, about Rs 13.5 + GST at many "
                 "brokers; on Rs 10,000 it alone is about 0.16%, on Rs 1,000 about 1.6%.",
        "verify": ["https://zerodha.com/charges", "https://www.nseindia.com/"],
    },
    "IN-delivery-flat-20": {
        "market": "IN", "label": "India, delivery, broker charging Rs 20 or 0.1% per order (whichever is lower)",
        "brokerage_pct": 0.1, "brokerage_cap": 20.0, "stt_buy_pct": 0.1, "stt_sell_pct": 0.1,
        "exchange_txn_pct": 0.00297, "sebi_pct": 0.0001, "stamp_buy_pct": 0.015, "gst_pct": 18.0,
        "dp_charge_per_sell": 15.93, "notes": "Typical of several app brokers; check yours.",
        "verify": ["https://groww.in/pricing"],
    },
    "IN-intraday": {
        "market": "IN", "label": "India, intraday (buy and sell the same day)",
        "brokerage_pct": 0.03, "brokerage_cap": 20.0, "stt_buy_pct": 0.0, "stt_sell_pct": 0.025,
        "exchange_txn_pct": 0.00297, "sebi_pct": 0.0001, "stamp_buy_pct": 0.003, "gst_pct": 18.0,
        "dp_charge_per_sell": 0.0, "notes": "No DP charge, but intraday profits are taxed as speculative business "
                                             "income at your slab rate.",
        "verify": ["https://zerodha.com/charges"],
    },
    "US-zero-commission": {
        "market": "US", "label": "US, zero-commission broker with fractional shares (e.g. Robinhood, Fidelity)",
        "brokerage_pct": 0.0, "brokerage_cap": 0.0, "sec_fee_sell_pct": 0.00278, "finra_taf_per_share": 0.000166,
        "finra_taf_cap": 8.30,
        "notes": "The SEC fee on sales is reset every fiscal year; FINRA's trading activity fee is per share sold. "
                 "On $100 both come to a cent or two. Payment for order flow means spreads are the hidden cost.",
        "verify": ["https://www.sec.gov/divisions/marketreg/sec-fee-rate", "https://www.finra.org/rules-guidance/"
                   "guidance/trading-activity-fee"],
    },
}

TAX: dict[str, dict] = {
    "IN": {
        "short_term_equity_pct": 20.0, "short_term_rule": "listed shares held 12 months or less",
        "long_term_equity_pct": 12.5, "long_term_exemption": 125000,
        "long_term_rule": "held over 12 months; the first Rs 1.25 lakh of long-term gains a year is exempt",
        "intraday": "speculative business income, taxed at your income slab",
        "nri": "for NRIs the broker deducts tax at source (TDS) on gains at these rates plus surcharge and cess; "
               "a DTAA with the country of residence can matter",
        "verify": ["https://incometaxindia.gov.in/"],
    },
    "US": {
        "short_term": "held one year or less: taxed as ordinary income (10% to 37% federal, plus state)",
        "long_term": "held over one year: 0%, 15% or 20% federal depending on income",
        "wash_sale": "a loss is disallowed if you rebuy the same stock within 30 days",
        "nonresident": "nonresident aliens (many F-1 and OPT holders) follow different rules; capital gains and "
                       "dividend withholding depend on residency status, so check with a tax professional",
        "verify": ["https://www.irs.gov/taxtopics/tc409", "https://www.irs.gov/individuals/international-taxpayers"],
    },
}


def _load_overrides() -> None:
    if OVERRIDE.exists():
        o = json.loads(OVERRIDE.read_text())
        for k, v in (o.get("profiles") or {}).items():
            PROFILES[k] = {**PROFILES.get(k, {}), **v}
        for k, v in (o.get("tax") or {}).items():
            TAX[k] = {**TAX.get(k, {}), **v}


_load_overrides()


def round_trip(profile: str, amount: float, price: float | None = None, gain_pct: float = 0.0) -> dict:
    """Charges to buy `amount` and sell it later at +gain_pct, excluding tax."""
    p = PROFILES[profile]
    buy, sell = amount, amount * (1 + gain_pct / 100)
    if p["market"] == "IN":
        brok = lambda v: min(v * p["brokerage_pct"] / 100, p["brokerage_cap"]) if p["brokerage_cap"] else 0.0  # noqa: E731
        b_brok, s_brok = brok(buy), brok(sell)
        txn = (buy + sell) * p["exchange_txn_pct"] / 100
        sebi = (buy + sell) * p["sebi_pct"] / 100
        parts = {"brokerage": b_brok + s_brok, "stt": buy * p["stt_buy_pct"] / 100 + sell * p["stt_sell_pct"] / 100,
                 "exchange_txn": txn, "sebi": sebi, "stamp_duty": buy * p["stamp_buy_pct"] / 100,
                 "gst": (b_brok + s_brok + txn + sebi) * p["gst_pct"] / 100, "dp_charge": p["dp_charge_per_sell"]}
    else:
        shares = amount / price if price else 1.0
        parts = {"commission": 0.0, "sec_fee": sell * p["sec_fee_sell_pct"] / 100,
                 "finra_taf": min(shares * p["finra_taf_per_share"], p["finra_taf_cap"])}
    total = sum(parts.values())
    return {"profile": profile, "label": p["label"], "amount": round(amount, 2),
            "charges": {k: round(v, 2) for k, v in parts.items()}, "total_charges": round(total, 2),
            "total_charges_pct_of_amount": round(total / amount * 100, 2) if amount else None,
            "breakeven_move_pct": round(total / amount * 100, 2) if amount else None, "notes": p["notes"]}


def goal_math(start: float, target: float) -> dict:
    """How hard is start -> target? Pure arithmetic, no forecasting."""
    mult = target / start
    need = lambda months: round((mult ** (1 / months) - 1) * 100, 1)  # noqa: E731
    years_at = {f"{r}%/yr": round(math.log(mult) / math.log(1 + r / 100), 1) for r in (8, 12, 15, 20, 30, 50)}
    return {"multiple": round(mult, 2), "gain_needed_pct": round((mult - 1) * 100, 1),
            "required_monthly_return_pct": {"in 1 month": need(1), "in 3 months": need(3), "in 12 months": need(12),
                                            "in 36 months": need(36)},
            "years_needed_at_steady_cagr": years_at,
            "context": "Broad indices have historically compounded around 8-12% a year in USD terms and somewhat more "
                       "in rupee terms, with deep drawdowns along the way. Returns high enough to 10x quickly exist "
                       "only in lottery-like bets (far out-of-the-money options, penny stocks, leveraged products) "
                       "where most participants lose most of their stake."}


def brief_block(market_code: str, budget: float, target: float | None = None) -> dict:
    code = "IN" if market_code == "IN" else "US"
    profiles = [k for k, v in PROFILES.items() if v["market"] == code]
    out = {"as_of": "published schedules as of mid-2026; approximate, verify before trading",
           "round_trip_on_budget": [round_trip(k, budget, gain_pct=0) for k in profiles],
           "tax": TAX[code]}
    if target:
        out["goal"] = goal_math(budget, target)
    return out
