"""How the council learns from being wrong.

This is learning from feedback, not retraining: the models' weights never change. What changes is
what they are told and how much they are trusted.

  1. Every stock vote and every ruling is recorded as a call with the price AND the market benchmark
     (S&P 500 or Nifty 50) at that moment. CASH is a call too.
  2. grade() marks every call to market. A call's score is its EXCESS return: the stock's return minus
     the benchmark's over the same span. Beating a rising market is the bar, not just being up.
     A call matures at its horizon; before that it is "live" and shown separately.
  3. Each seat's matured record becomes a trust weight (shrunk toward 1.0 until it has a real sample),
     and the vote tally is multiplied by it: seats that keep being right count for more.
  4. reflect() hands the matured calls, with the reasons each seat gave, to a free-tier model that
     writes short lessons ("momentum picks into earnings lost 3 of 4"). Lessons go into every future
     prompt: council-wide ones to all seats, seat-specific ones to that seat, with its own record.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime

from . import llm, market, store

BENCH = {"US": "^GSPC", "IN": "^NSEI"}
REFLECT_MODELS = ["nvidia/nvidia/nemotron-3-ultra-550b-a55b", "nvidia/nvidia/nemotron-3-super-120b-a12b",
                  "gemini/gemini-3.1-flash-lite"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, ts REAL, seat TEXT, model TEXT, kind TEXT,
  market TEXT, ticker TEXT, confidence REAL, reason TEXT, price REAL, bench TEXT, bench_price REAL,
  horizon_end TEXT, last_price REAL, last_bench REAL, ret_pct REAL, bench_ret_pct REAL, excess_pct REAL,
  graded REAL, matured INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS lessons (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, applies_to TEXT, lesson TEXT, evidence TEXT, active INTEGER
);
"""


def _conn():
    c = store.conn()
    c.executescript(SCHEMA)
    return c


def record_calls(sid: str, market_code: str, horizon_end: str, votes: dict[str, dict], ruling: dict,
                 chair_name: str, prices: dict[str, float]) -> None:
    bench = BENCH.get(market_code, "^GSPC")
    bench_px = None
    for _ in range(3):  # a call without its benchmark price can never be graded, so retry before giving up
        try:
            bench_px = market.price_now(bench)
            break
        except Exception:  # noqa: BLE001
            time.sleep(3)
    rows = []
    for seat, v in votes.items():
        rows.append((seat, v.get("_model", ""), "vote", v.get("vote"), v.get("confidence"), v.get("reason", "")))
    rows.append((chair_name, ruling.get("_model", ""), "ruling", ruling.get("decision"), ruling.get("confidence"),
                 ruling.get("why", "")))
    with store._lock, _conn() as c:
        for seat, model, kind, ticker, conf, reason in rows:
            try:
                conf = float(conf)
            except (TypeError, ValueError):
                conf = None
            c.execute("INSERT INTO calls (session, ts, seat, model, kind, market, ticker, confidence, reason, price, "
                      "bench, bench_price, horizon_end) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (sid, time.time(), seat, model, kind, market_code, ticker, conf, (reason or "")[:600],
                       prices.get(ticker), bench, bench_px, horizon_end))


def grade() -> int:
    """Mark every unmatured call to market. Returns how many calls matured in this pass."""
    with _conn() as c:
        open_calls = [dict(r) for r in c.execute("SELECT * FROM calls WHERE matured=0").fetchall()]
    if not open_calls:
        return 0
    px: dict[str, float | None] = {}
    for sym in {r["ticker"] for r in open_calls if r["ticker"] not in (None, "CASH")} | {r["bench"] for r in open_calls}:
        try:
            px[sym] = market.price_now(sym)
        except Exception:  # noqa: BLE001
            px[sym] = None
    today, matured = date.today(), 0
    with store._lock, _conn() as c:
        for r in open_calls:
            b_now = px.get(r["bench"])
            if not (b_now and r["bench_price"]):
                continue
            b_ret = (b_now / r["bench_price"] - 1) * 100
            if r["ticker"] == "CASH":
                s_now, ret = None, 0.0
            else:
                s_now = px.get(r["ticker"])
                if not (s_now and r["price"]):
                    continue
                ret = (s_now / r["price"] - 1) * 100
            done = int(bool(r["horizon_end"]) and today > date.fromisoformat(r["horizon_end"]))
            matured += done
            c.execute("UPDATE calls SET last_price=?, last_bench=?, ret_pct=?, bench_ret_pct=?, excess_pct=?, graded=?, "
                      "matured=? WHERE id=?", (s_now, b_now, round(ret, 3), round(b_ret, 3), round(ret - b_ret, 3),
                                               time.time(), done, r["id"]))
    return matured


def seat_stats() -> dict[str, dict]:
    with _conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM calls WHERE excess_pct IS NOT NULL").fetchall()]
    out: dict[str, dict] = {}
    for r in rows:
        s = out.setdefault(r["seat"], {"seat": r["seat"], "matured": [], "live": []})
        s["matured" if r["matured"] else "live"].append(r["excess_pct"])
    for s in out.values():
        m, lv = s.pop("matured"), s.pop("live")
        s["matured_calls"], s["live_calls"] = len(m), len(lv)
        s["matured_avg_excess_pct"] = round(sum(m) / len(m), 2) if m else None
        s["matured_beat_market_pct"] = round(100 * sum(x > 0 for x in m) / len(m), 0) if m else None
        s["live_avg_excess_pct"] = round(sum(lv) / len(lv), 2) if lv else None
        s["trust_weight"] = trust_weight(m)
    return out


def trust_weight(matured_excess: list[float]) -> float:
    """1.0 with no record; moves toward 0.5 to 1.5 as matured calls accumulate (shrinkage k=5)."""
    n = len(matured_excess)
    if not n:
        return 1.0
    avg = sum(matured_excess) / n
    raw = max(-0.5, min(0.5, avg / 5))  # 5 points of average excess return saturates the weight
    return round(1 + raw * n / (n + 5), 3)


def weights() -> dict[str, float]:
    return {k: v["trust_weight"] for k, v in seat_stats().items()}


def active_lessons(seat: str | None = None) -> list[dict]:
    with _conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM lessons WHERE active=1 ORDER BY id DESC").fetchall()]
    return [r for r in rows if r["applies_to"] in ("all", seat)] if seat else rows


def memory_for(seat: str) -> str:
    """The block a seat reads before it speaks: its own record and what the council has learned."""
    st = seat_stats().get(seat)
    lines = ["YOUR TRACK RECORD (excess return = your pick's return minus the benchmark's): "]
    if not st or not (st["matured_calls"] or st["live_calls"]):
        lines[0] += "no graded calls yet."
    else:
        lines[0] += (f"{st['matured_calls']} matured calls, avg excess {st['matured_avg_excess_pct']}%, beat the "
                     f"market {st['matured_beat_market_pct']}% of the time; {st['live_calls']} live calls averaging "
                     f"{st['live_avg_excess_pct']}% so far. Your vote weight is {st['trust_weight']}.")
    ls = active_lessons(seat)
    if ls:
        lines.append("LESSONS FROM PAST OUTCOMES (apply them; they came from real results):")
        lines += [f"- {x['lesson']}" + ("" if x["applies_to"] == "all" else " (for you)") for x in ls[:10]]
    return "\n".join(lines)


def reflect(min_new: int = 1) -> list[dict]:
    """Turn matured calls into lessons. Uses a free-tier model; never a rationed seat."""
    with _conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM calls WHERE matured=1 ORDER BY ts DESC LIMIT 60").fetchall()]
        last = c.execute("SELECT MAX(ts) FROM lessons").fetchone()[0] or 0
    fresh = [r for r in rows if (r["graded"] or 0) > last]
    if len(fresh) < min_new or not rows:
        return []
    evidence = [{"seat": r["seat"], "kind": r["kind"], "market": r["market"], "ticker": r["ticker"],
                 "confidence": r["confidence"], "reason_given": r["reason"][:300],
                 "return_pct": r["ret_pct"], "benchmark_pct": r["bench_ret_pct"], "excess_pct": r["excess_pct"],
                 "called": datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d")} for r in rows]
    prior = [x["lesson"] for x in active_lessons()]
    prompt = (f"GRADED CALLS (newest first)\n{json.dumps(evidence)}\n\nCURRENT LESSONS\n{json.dumps(prior)}\n\n"
              "TASK: You are the council's post-mortem analyst. Find patterns in what beat or lost to the market, "
              "comparing the reasons given with what happened. Write at most 8 lessons, each one sentence, each "
              "backed by the calls it came from. Keep current lessons the evidence still supports, drop ones it "
              "contradicts. With fewer than 5 matured calls, say lessons are tentative. Never invent calls.\n"
              'JSON: {"lessons": [{"lesson": "", "applies_to": "all or an exact seat name", '
              '"evidence": "tickers and outcomes"}]}')
    for model in REFLECT_MODELS:
        try:
            d = llm.parse_json(llm.chat(model, "You write short, evidence-bound trading lessons. JSON only.",
                                        prompt, max_tokens=2500)["text"])
        except llm.LLMError:
            continue
        if isinstance(d, dict) and isinstance(d.get("lessons"), list):
            new = [x for x in d["lessons"] if isinstance(x, dict) and x.get("lesson")][:8]
            with store._lock, _conn() as c:
                c.execute("UPDATE lessons SET active=0")
                for x in new:
                    c.execute("INSERT INTO lessons (ts, applies_to, lesson, evidence, active) VALUES (?,?,?,?,1)",
                              (time.time(), x.get("applies_to") or "all", x["lesson"][:400],
                               str(x.get("evidence", ""))[:400]))
            return new
    return []


def track_record() -> dict:
    """Everything a skeptic needs: every ruling, what it returned, and what the index did meanwhile."""
    with _conn() as c:
        rulings = [dict(r) for r in c.execute("SELECT * FROM calls WHERE kind='ruling' ORDER BY ts DESC").fetchall()]
    graded = [r for r in rulings if r["excess_pct"] is not None]
    mat = [r for r in graded if r["matured"]]

    def summary(rs):
        if not rs:
            return None
        return {"calls": len(rs), "avg_return_pct": round(sum(r["ret_pct"] for r in rs) / len(rs), 2),
                "avg_benchmark_pct": round(sum(r["bench_ret_pct"] for r in rs) / len(rs), 2),
                "avg_excess_pct": round(sum(r["excess_pct"] for r in rs) / len(rs), 2),
                "beat_market_pct": round(100 * sum(r["excess_pct"] > 0 for r in rs) / len(rs), 0)}
    return {"matured": summary(mat), "all_graded": summary(graded),
            "rulings": [{k: r[k] for k in ("session", "ts", "seat", "market", "ticker", "confidence", "price",
                                           "bench", "bench_price", "horizon_end", "last_price", "ret_pct",
                                           "bench_ret_pct", "excess_pct", "matured")} for r in rulings[:200]],
            "seats": list(seat_stats().values()), "lessons": active_lessons()}
