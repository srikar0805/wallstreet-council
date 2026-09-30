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
