"""Offline tests: no network, no model calls."""
import os
import tempfile

os.environ["COUNCIL_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")

from wallstreet_council import llm, store  # noqa: E402
from wallstreet_council.council import Council  # noqa: E402


def test_parse_json_variants():
    assert llm.parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm.parse_json('<think>{"no": 1}</think> sure: {"vote": "NVDA", "why": "a \\"quoted\\" }"}') == \
        {"vote": "NVDA", "why": 'a "quoted" }'}
    assert llm.parse_json("no json here") is None


def test_quant_band_and_cash():
    c = Council(budget=10, seats=[{"name": "X", "model": "nvidia/x", "role": "r", "focus": "f"}])
    c.brief = {"clock": {"trading_days_left_incl_today": 21, "month_end": "2026-10-31"},
               "candidates": [{"symbol": "AAA", "price": 100.0, "vol_annual_pct": 40.0}]}
    q = c.quant_check({"decision": "AAA", "horizon_return_pct": {"base": 2}})
    assert q["value_1sd_low"] < 10 < q["value_1sd_high"]
    assert q["value_2sd_low"] < q["value_1sd_low"]
    assert q["council_base_value"] == 10.2
    assert c.quant_check({"decision": "CASH"})["value_mid"] == 10


def test_store_roundtrip():
    store.create_session("s1", 10, "h", [])
    store.add_event("s1", "vote", "Seat", "vote", "I vote AAA", "m", {"vote": "AAA"})
    ev = store.events("s1")
    assert ev[-1]["data"] == {"vote": "AAA"}
    store.finish_session("s1", "done", {"decision": "AAA"})
    assert store.session("s1")["verdict"]["decision"] == "AAA"


def test_horizon_end():
    from datetime import date
    from wallstreet_council.market import horizon_end
    t = date(2026, 9, 29)
    assert horizon_end("end of this month", t) == date(2026, 9, 30)
    assert horizon_end("end of October 2026", t) == date(2026, 10, 31)
    assert horizon_end("end of next month", t) == date(2026, 10, 31)
    assert horizon_end("end of March", t) == date(2027, 3, 31)


def test_costs_india_delivery_dp_charge_dominates():
    from wallstreet_council import costs
    r = costs.round_trip("IN-delivery-zero-brokerage", 1000)
    assert r["charges"]["dp_charge"] == 15.93
    assert 1.7 < r["total_charges_pct_of_amount"] < 2.0
    assert costs.round_trip("US-zero-commission", 10, price=100)["total_charges"] == 0.0


def test_goal_math_10x():
    from wallstreet_council import costs
    g = costs.goal_math(10, 100)
    assert g["gain_needed_pct"] == 900.0
    assert g["required_monthly_return_pct"]["in 1 month"] == 900.0
    assert 20 < g["years_needed_at_steady_cagr"]["12%/yr"] < 21


def test_trust_weight_shrinks_toward_one():
    from wallstreet_council.learning import trust_weight
    assert trust_weight([]) == 1.0
    assert 1.0 < trust_weight([5.0]) < trust_weight([5.0] * 20) <= 1.5
    assert trust_weight([-10.0] * 20) >= 0.5


def test_schedule_parse():
    from wallstreet_council.floor import parse_schedule
    s = parse_schedule("pick-US@09:05, ipo-IN@12:30")
    assert s[1] == {"mode": "ipo", "market": "IN", "hh": 12, "mm": 30, "key": "ipo-IN@12:30"}


def test_phone_gateway_requires_login(monkeypatch):
    from starlette.testclient import TestClient
    from wallstreet_council import phone
    monkeypatch.setattr(phone, "password", lambda rotate=False: "test-pass-word-1234")
    monkeypatch.setattr(phone, "_secret", lambda: b"x" * 32)
    phone._fails.clear()
    phone._all_fails.clear()
    c = TestClient(phone.build_app(), base_url="https://testserver")
    assert c.get("/", follow_redirects=False).status_code == 303
    assert c.get("/api/sessions").status_code == 401
    assert c.post("/login", data={"password": "wrong"}).status_code == 401
    assert c.post("/login", data={"password": "test-pass-word-1234"}, follow_redirects=False).status_code == 303
    assert c.get("/api/sessions").status_code == 200
    assert c.get("/api/phone").status_code == 404  # the password endpoint never exists on the gateway
    for _ in range(5):
        c.post("/login", data={"password": "wrong"})
    assert c.post("/login", data={"password": "wrong"}).status_code == 429


PTR_SAMPLE = """ID Owner Asset Transaction
Type
Date Notification
Date
Amount Cap.
Gains >
$200?
SP Alphabet Inc. - Class A Common
Stock (GOOGL) [ST]
P 01/16/2026 01/16/2026 $500,001 -
$1,000,000
F      S     : New
D          : Exercised 50 call options purchased 1/14/25 (5,000 shares) at a strike price of $150.
JT Cheniere Energy, Inc. Common Stock
(LNG) [ST]
S 12/12/2025 01/07/2026 $1,001 - $15,000
F      S     : New
S          O : Morgan Stanley - Select UMA Account # 1
AT&T Inc. (T) [ST] S (partial) 03/16/2026 03/16/2026 $1,001 - $15,000
"""


def test_parse_electronic_ptr():
    from wallstreet_council.congress import parse_electronic
    rows = parse_electronic(PTR_SAMPLE)
    assert [(r["owner"], r["ticker"], r["tx_type"]) for r in rows] == [
        ("spouse", "GOOGL", "buy"), ("joint", "LNG", "sell"), ("self", "T", "partial sell")]
    assert rows[0]["amount_min"] == 500001 and rows[0]["amount_max"] == 1000000
    assert rows[1]["tx_date"] == "2025-12-12" and rows[1]["notified_date"] == "2026-01-07"
    assert rows[0]["asset"] == "Alphabet Inc. - Class A Common Stock"


def test_fair_test_has_no_lookahead(monkeypatch):
    """A's stock soars before the selection date and crashes after; B's is flat throughout.
    Ranking must use only pre-selection prices, so A is followed (and then loses)."""
    import pandas as pd
    from wallstreet_council import congress
    days = pd.bdate_range("2024-01-01", "2025-06-30")
    up_then_down = [100 + i if d < pd.Timestamp("2025-01-01") else 400 - i for i, d in enumerate(days)]
    px = pd.DataFrame({"AAA": up_then_down, "BBB": [100.0] * len(days), "SPY": [100.0] * len(days)}, index=days)
    monkeypatch.setattr(congress, "_prices", lambda tickers, start: px)
    with congress._conn() as c:
        c.execute("DELETE FROM gov_trades")
        for filer, tk in (("A", "AAA"), ("B", "BBB")):
            for d in ("2024-01-02", "2025-02-03"):
                c.execute("INSERT INTO gov_trades (filer, ticker, tx_type, asset_type, filed_date) VALUES (?,?,?,?,?)",
                          (filer, tk, "buy", "ST", d))
    r = congress.fair_test(top_n=1, min_trades=1)
    first = r["periods"][0]
    assert first["start"] == "2025-01-01"
    assert [f["filer"] for f in first["followed"]] == ["A"]   # chosen on pre-2025 prices only
    assert first["top"]["avg_excess_pct"] < 0                # and the later crash is what the test reports
