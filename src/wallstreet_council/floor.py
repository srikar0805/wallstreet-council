"""The trading floor: the council talking continuously, on a small budget.

Two loops share one process:

  Floor chatter   every few minutes, three seats from the free-tier bench (NVIDIA, Gemini) take turns
                  reacting to fresh tape and new headlines and to each other, by name. One rolling
                  conversation per US Eastern day, session id `floor-YYYY-MM-DD`.
  Full councils   at fixed Eastern times on weekdays (default 09:05 and 15:30) the whole protocol runs.
                  Only here does a rationed subscription seat speak: Codex chairs, at most
                  COUNCIL_CODEX_DAILY times a day (default 2). Past the ration, the fallback chair rules.

Codex and Claude never take part in floor chatter, so continuous talk costs nothing on the ChatGPT plan.
"""
from __future__ import annotations

import itertools
import json
import os
import signal
import time
from datetime import datetime
from pathlib import Path

from . import llm, market, store
from .council import Council, load_seats

HOME = Path(os.environ.get("COUNCIL_HOME", Path.home() / ".wallstreet-council"))
PID_FILE = HOME / "live.pid"
LOG_FILE = HOME / "live.log"
RATIONED = ("codex", "claude")

FLOOR_RULES = """You are on the trading floor of the Wall Street Council, a PAPER-TRADING simulation. The floor
is a running conversation between AI analysts between full council meetings.
- React to the latest tape and headlines, and to the previous speaker BY NAME: agree, push back, or add.
- Use only numbers from the FLOOR DATA or the conversation. Never invent figures.
- 2 to 4 sentences, plain talk, like a desk chat. No disclaimers, no lists.
- Reply with ONE JSON object: {"message": "", "mood": "bullish|bearish|neutral", "watch": ["TICKER"]}"""


def floor_id() -> str:
    return "floor-" + datetime.now(market.ET).strftime("%Y-%m-%d")


def tape() -> dict:
    """Cheap refresh: index moves, today's movers, and a handful of headlines."""
    syms = {"SPY": "S&P 500", "QQQ": "Nasdaq 100", "IWM": "Russell 2000", "^VIX": "VIX", "^TNX": "10y yield",
            "CL=F": "Crude oil", "BTC-USD": "Bitcoin"}
    regime = {}
    for s, name in syms.items():
        try:
            d = market.snapshot(s)
            regime[name] = {k: d.get(k) for k in ("price", "chg_1d", "chg_5d")}
        except Exception:  # noqa: BLE001
            pass
    movers = []
    for s in market.screened(limit=4)[:8]:
        try:
            d = market.snapshot(s)
            movers.append({k: d.get(k) for k in ("symbol", "price", "chg_1d", "chg_5d", "rsi14", "volume_vs_20d")})
        except Exception:  # noqa: BLE001
            pass
    news = []
    for q in ("stock market today", "stocks moving premarket OR after hours", "Federal Reserve",
              "sports business stocks", "earnings results today"):
        news += market.headlines(q, n=4, window="6h")
    return {"clock": market.market_clock(), "regime": regime, "movers": movers, "news": news}


class Floor:
    def __init__(self, interval_open: int = 15, interval_closed: int = 60, council_times: str = "09:05,15:30",
                 budget: float = 10.0, speakers: int = 3):
        self.interval_open, self.interval_closed = interval_open * 60, interval_closed * 60
        self.council_times = [t.strip() for t in council_times.split(",") if t.strip()]
        self.budget, self.speakers = budget, speakers
        bench = [s for s in load_seats() if s["model"].split("/")[0] not in RATIONED]
        self.bench = itertools.cycle(bench)
        self.seen: set[str] = set()
        self.ran_councils: set[str] = set()
        self.stop = False

    # ---- floor chatter --------------------------------------------------------------------------
    def ensure_session(self) -> str:
        sid = floor_id()
        store.create_session(sid, self.budget, "continuous floor", [], status="live")
        return sid

    def tick(self) -> None:
        sid = self.ensure_session()
        t = tape()
        fresh = [h for h in t["news"] if h["title"] not in self.seen]
        self.seen.update(h["title"] for h in fresh)
        store.add_event(sid, "floor", "Tape", "brief",
                        "Tape: " + ", ".join(f"{k} {v.get('chg_1d'):+}%" for k, v in t["regime"].items()
                                             if v.get("chg_1d") is not None)
                        + (f" | movers: " + ", ".join(f"{m['symbol']} {m['chg_1d']:+}%" for m in t["movers"]
                                                       if m.get("chg_1d") is not None) if t["movers"] else "")
                        + f" | {len(fresh)} new headlines", data={"clock": t["clock"]})
        data = json.dumps({"clock": t["clock"], "regime": t["regime"], "movers": t["movers"],
                           "new_headlines": [f"({h['sentiment']:+.2f}) {h['title']}" for h in fresh[:12]]})
        last_ruling = next((s["verdict"] for s in store.sessions(20)
                            if s["verdict"] and not s["id"].startswith("floor-")), None)
        for _ in range(self.speakers):
            seat = next(self.bench)
            convo = "\n".join(f"{e['speaker']}: {e['text']}" for e in store.last_events(sid, 10)
                              if e["kind"] in ("chat", "ruling", "brief"))
            prompt = (f"FLOOR DATA\n{data}\n\nLAST COUNCIL RULING\n"
                      f"{json.dumps({k: (last_ruling or {}).get(k) for k in ('decision', 'entry_window_et', 'why')})}"
                      f"\n\nCONVERSATION SO FAR\n{convo or '(you open the floor)'}\n\nYour turn.")
            system = f"{FLOOR_RULES}\n\nYou are {seat['name']}, the {seat['role']}. Lens: {seat['focus']}."
            reply = None
            for model in [seat["model"], *seat.get("fallback", [])]:
                try:
                    r = llm.chat(model, system, prompt, max_tokens=1500, timeout=90)
                    d = llm.parse_json(r["text"])
                    reply = (d if isinstance(d, dict) and d.get("message") else {"message": r["text"][:800]}, model)
                    break
                except llm.LLMError:
                    continue
            if reply:
                d, model = reply
                store.add_event(sid, "floor", seat["name"], "chat", d["message"], model, d)
            if self.stop:
                return

    # ---- scheduled councils ---------------------------------------------------------------------
    def due_council(self) -> str | None:
        now = datetime.now(market.ET)
        if now.weekday() >= 5:
            return None
        for hhmm in self.council_times:
            key = f"{now:%Y-%m-%d} {hhmm}"
            h, m = map(int, hhmm.split(":"))
            mins = (now.hour * 60 + now.minute) - (h * 60 + m)
            if 0 <= mins < 30 and key not in self.ran_councils:
                return key
        return None

    def run_council(self, key: str) -> None:
        self.ran_councils.add(key)
        sid = self.ensure_session()
        left = llm.budget_left("codex")
        c = Council(budget=self.budget, rounds=1)
        store.add_event(sid, "floor", "Moderator", "status",
                        f"Full council convened ({key} ET), session {c.id}. Codex rulings left today: {left}.")
        try:
            ruling = c.run()
            store.add_event(sid, "floor", "Council", "ruling",
                            f"Council ruling: {ruling['decision']}. {ruling.get('why', '')}", data=ruling)
        except Exception as e:  # noqa: BLE001
            store.add_event(sid, "floor", "Moderator", "error", f"Council failed: {e}")

    # ---- main loop ------------------------------------------------------------------------------
    def run(self) -> None:
        store.fail_stale_sessions()
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stop", True))
        while not self.stop:
            key = self.due_council()
            if key:
                self.run_council(key)
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                store.add_event(self.ensure_session(), "floor", "Moderator", "error", f"floor tick failed: {e}")
            wait = self.interval_open if market.market_clock()["market_open"] else self.interval_closed
            end = time.time() + wait
            while time.time() < end and not self.stop and not self.due_council():
                time.sleep(5)


# ---- process control, used by the CLI and the MCP server ----------------------------------------
def running_pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def start_detached(**kw) -> int:
    import subprocess
    import sys
    pid = running_pid()
    if pid:
        return pid
    HOME.mkdir(parents=True, exist_ok=True)
    args = [sys.executable, "-m", "wallstreet_council.cli", "live"]
    for k, v in kw.items():
        args += [f"--{k.replace('_', '-')}", str(v)]
    with open(LOG_FILE, "ab") as log:
        p = subprocess.Popen(args, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    return p.pid


def stop_detached() -> bool:
    pid = running_pid()
    if not pid:
        return False
    os.kill(pid, signal.SIGTERM)
    return True
