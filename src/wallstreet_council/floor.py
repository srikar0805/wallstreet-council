"""The trading floor: the council talking continuously, on a small budget, about both markets.

One process runs:

  Floor chatter   every few minutes, three seats from the free-tier bench (NVIDIA, Gemini) take turns
                  reacting to the fresh US and Indian tape, today's movers, live IPO subscription figures,
                  new headlines, and to each other, by name. One rolling conversation per US Eastern day,
                  session id `floor-YYYY-MM-DD`.
  Your messages   anything posted to the floor (monitor box, `council say`, MCP say_to_floor) wakes the
                  floor at once, and the next speakers answer you first.
  Full councils   on a schedule (default: US pick 09:05 ET, India pick 09:20 IST, India IPOs 12:30 IST,
                  weekdays). Only here does a rationed subscription seat speak: Codex chairs, at most
                  COUNCIL_CODEX_DAILY times a day (default 2); past that the fallback chair rules.
  Learning        every hour, calls are graded against the index; once a day, matured calls become lessons.
  Publishing      if COUNCIL_PUBLISH=1, a read-only snapshot is pushed to GitHub Pages every 15 minutes.

Codex and Claude never take part in floor chatter.
"""
from __future__ import annotations

import itertools
import json
import os
import signal
import time
from datetime import datetime
from pathlib import Path

from . import costs, dossier
from . import ipo as ipo_desk
from . import learning, llm, market, memory, store
from .council import Council, load_seats

HOME = Path(os.environ.get("COUNCIL_HOME", Path.home() / ".wallstreet-council"))
PID_FILE = HOME / "live.pid"
LOG_FILE = HOME / "live.log"
RATIONED = ("codex", "claude", "copilot")
DEFAULT_SCHEDULE = "pick-US@09:05, pick-IN@09:20, ipo-IN@12:30/Mon"  # IPOs weekly; a lot rarely fits the budget

FLOOR_RULES = """You are on the trading floor of the Wall Street Council, a PAPER-TRADING simulation covering US and
Indian markets and IPOs. The floor is a running conversation between AI analysts between full council meetings.
- If the CLIENT has just said something, answer the client first, directly. The CLIENT PROFILE holds everything
  the client has ever told us; use it. If it lists open_questions, make sure each gets a real answer over the
  next rounds.
- TODAY SO FAR is the digest of this floor's conversation: build on it, do not repeat it.
- Keep the client's GOAL in view: turning a small stake into ten times as much. Stay on stocks the client can
  actually buy and the latest Pick of the Day. Weigh routes (compounding, single stocks, intraday, options, penny
  stocks) with honest odds. IPOs are covered by the weekly IPO council; discuss them only if the client asks.
  Count broker charges, depository charges, transaction taxes and capital-gains tax (COSTS); name the account type.
- Share price is no barrier in the US: zero-commission US brokers sell fractional shares, so the US budget buys a
  slice of any stock. NSE and BSE trade whole shares only, so the rupee budget only buys stocks priced below it.
- Each round has a SPOTLIGHT stock with its full history. Say something specific about it when it is your turn.
- Otherwise react to the latest tape, IPO figures and headlines, and to the previous speaker BY NAME: agree,
  push back, or add.
- Use only numbers from the FLOOR DATA or the conversation. Never invent figures.
- 2 to 4 sentences, plain talk, like a desk chat. No disclaimers, no lists.
- Reply with ONE JSON object: {"message": "", "mood": "bullish|bearish|neutral", "watch": ["TICKER"]}"""


def floor_id() -> str:
    return "floor-" + datetime.now(market.ET).strftime("%Y-%m-%d")


def parse_schedule(spec: str) -> list[dict]:
    """'pick-US@09:05, ipo-IN@12:30/Mon' -> [{mode, market, hh, mm[, days]}]. Times are in that market's own zone;
    an optional /Mon,Thu suffix limits the weekdays."""
    names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    out = []
    for item in [x.strip() for x in spec.split(",") if "@" in x]:
        what, _, at = item.partition("@")
        at, _, days = at.partition("/")
        mode, _, code = what.partition("-")
        h, m = map(int, at.split(":"))
        e = {"mode": mode.lower(), "market": (code or "US").upper(), "hh": h, "mm": m, "key": item}
        if days:
            e["days"] = [names.index(d.strip().lower()[:3]) for d in days.split("+") if d.strip()]
        out.append(e)
    return out


def say_to_floor(text: str, who: str = "Client") -> int:
    sid = floor_id()
    store.create_session(sid, 0, "continuous floor", [], status="live", mode="floor", market="BOTH")
    return store.add_event(sid, "floor", who, "user", text.strip()[:1000])


def tape() -> dict:
    """Cheap refresh across both markets."""
    regime = {}
    for s, name in {"SPY": "S&P 500", "QQQ": "Nasdaq 100", "^VIX": "VIX", "^TNX": "US 10y", "^NSEI": "Nifty 50",
                    "^BSESN": "Sensex", "^NSEBANK": "Bank Nifty", "^INDIAVIX": "India VIX", "USDINR=X": "USD/INR",
                    "BZ=F": "Brent"}.items():
        d = market.snapshot(s)
        if not d.get("error"):
            regime[name] = {k: d.get(k) for k in ("price", "chg_1d", "chg_5d")}
    movers = []
    for code in ("US", "IN"):
        for s in market.screened(code, limit=3)[:6]:
            d = market.snapshot(s)
            if not d.get("error"):
                movers.append({k: d.get(k) for k in ("symbol", "price", "chg_1d", "chg_5d", "rsi14", "volume_vs_20d")})
    ipo_live: list = []  # IPOs belong to the weekly IPO council now
    news = []
    for q, ed in (("stock market today", "US"), ("Sensex Nifty today", "IN"), ("Federal Reserve", "US"),
                  ("RBI FII flows", "IN"), ("earnings results today", "US"), ("Nifty stocks results today", "IN")):
        news += [{**h, "edition": ed} for h in market.headlines(q, n=4, window="6h", edition=ed)]
    return {"clock_us": market.market_clock("US"), "clock_in": market.market_clock("IN"), "regime": regime,
            "movers": movers, "india_ipos_open": ipo_live, "news": news}


def _salvage(text: str) -> str:
    """A reply cut off mid-JSON still carries its message; never show raw JSON to a reader."""
    import re
    m = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)', text, re.S)
    msg = m.group(1).replace('\\"', '"').replace("\\n", " ") if m else text
    msg = msg.strip()
    if len(msg) > 900 or (m and not text.rstrip().endswith("}")):
        cut = max(msg[:900].rfind(". "), msg[:900].rfind("? "))
        msg = msg[:cut + 1] if cut > 200 else msg[:900] + "…"
    return msg


class Floor:
    def __init__(self, interval_open: int = 15, interval_closed: int = 60, schedule: str = DEFAULT_SCHEDULE,
                 budget: float | None = None, speakers: int = 3, publish: bool | None = None, goal: float | None = None):
        self.interval_open, self.interval_closed = interval_open * 60, interval_closed * 60
        self.schedule = parse_schedule(schedule)
        self.budget, self.speakers, self.goal = budget, speakers, goal
        self.spot_queue: list[str] = []
        usd_inr = market.snapshot("USDINR=X").get("price") or 90.0
        from .council import DEFAULT_BUDGET
        start, start_in = budget or DEFAULT_BUDGET["US"], DEFAULT_BUDGET["IN"]
        goal = goal or start * 10
        self.budget_in = start_in
        self.goal_block = {
            "client_goal": f"turn ${start:,.0f} into ${goal:,.0f} in the US, and Rs {start_in:,.0f} into Rs {start_in * goal / start:,.0f} in India",
            "goal_math": costs.goal_math(start, goal),
            "costs_us": costs.round_trip("US-zero-commission", start),
            "costs_india_delivery": costs.round_trip("IN-delivery-zero-brokerage", start_in),
            "costs_india_intraday": costs.round_trip("IN-intraday", start_in),
            "tax": costs.TAX,
        }
        bench = [s for s in load_seats() if s["model"].split("/")[0] not in RATIONED]
        self.bench = itertools.cycle(bench)
        self.seen: set[str] = set()
        self.ran: set[str] = set()
        self.stop = False
        self.last_user_seq = 0
        self.last_grade = self.last_publish = 0.0
        self.last_reflect_day = ""
        self.publish = publish if publish is not None else os.environ.get("COUNCIL_PUBLISH") == "1"

    def ensure_session(self) -> str:
        sid = floor_id()
        store.create_session(sid, 0, "continuous floor", [], status="live", mode="floor", market="BOTH")
        return sid

    def new_user_messages(self) -> list[dict]:
        evs = [e for e in store.last_events(self.ensure_session(), 30)
               if e["kind"] == "user" and e["seq"] > self.last_user_seq]
        return evs

    # ---- floor chatter --------------------------------------------------------------------------
    def spotlight(self, movers: list[dict], client_text: str) -> dict | None:
        """Next stock in the rotation: anything the client named first, then movers, then both core lists,
        so over the days the floor works through every name."""
        import re
        named = [w for w in re.findall(r"\b[A-Z][A-Z&]{1,11}\b", client_text)
                 if w not in {"IPO", "GMP", "RBI", "SEBI", "US", "USD", "INR", "NSE", "BSE", "ETF", "SME", "FII"}]
        if not self.spot_queue:
            # Only names the budget can actually buy: rulings and backup picks, the floor's watchlist, US movers
            # and core names (fractional shares), and Indian names priced under the rupee budget.
            picks = []
            for sess in store.sessions(20):
                v = sess.get("verdict") or {}
                picks += [t for t in (v.get("decision"), v.get("backup_pick")) if t and t not in ("CASH", "NONE")]
            watch = [w.get("ticker") for w in (memory.floor_digest(floor_id()).get("watchlist") or [])
                     if isinstance(w, dict) and w.get("ticker")]
            inr = self.budget_in
            india = [m["symbol"] for m in movers if m["symbol"].endswith(".NS")] + market.MARKETS["IN"]["core"]
            india = [t for t in india if (market.snapshot(t).get("price") or 1e9) <= inr]
            us = [m["symbol"] for m in movers if not m["symbol"].endswith((".NS", ".BO"))] + market.MARKETS["US"]["core"]
            self.spot_queue = list(dict.fromkeys(picks + watch + us + india))
        for w in reversed(named):
            for sym in (w, f"{w}.NS"):
                if not market.snapshot(sym).get("error"):
                    self.spot_queue.insert(0, sym)
                    break
        while self.spot_queue:
            sym = self.spot_queue.pop(0)
            d = dossier.build(sym, "IN" if sym.endswith((".NS", ".BO")) else "US")
            if not d.get("error"):
                return d
        return None

    def tick(self) -> None:
        sid = self.ensure_session()
        user_msgs = self.new_user_messages()
        if user_msgs:
            self.last_user_seq = user_msgs[-1]["seq"]
        t = tape()
        fresh = [h for h in t["news"] if h["title"] not in self.seen]
        self.seen.update(h["title"] for h in fresh)
        chg = lambda v: f"{v['chg_1d']:+}%" if v.get("chg_1d") is not None else "n/a"  # noqa: E731
        store.add_event(sid, "floor", "Tape", "brief",
                        "Tape: " + ", ".join(f"{k} {chg(v)}" for k, v in t["regime"].items())
                        + (" | movers: " + ", ".join(f"{m['symbol']} {chg(m)}" for m in t["movers"]) if t["movers"] else "")
                        + (" | India IPOs open: " + ", ".join(f"{x['company']} {x['subscribed_times']}x"
                                                               for x in t["india_ipos_open"]) if t["india_ipos_open"] else "")
                        + f" | {len(fresh)} new headlines", data={"clock_us": t["clock_us"], "clock_in": t["clock_in"]})
        data = json.dumps({k: t[k] for k in ("clock_us", "clock_in", "regime", "movers", "india_ipos_open")}
                          | {"new_headlines": [f"[{h['edition']}] ({h['sentiment']:+.2f}) {h['title']}"
                                               for h in fresh[:14]]})
        client_text = " ".join(m["text"] for m in user_msgs)
        spot = self.spotlight(t["movers"], client_text)
        if spot:
            hh = spot.get("history", {})
            store.add_event(sid, "floor", "Research desk", "brief",
                            f"Spotlight: {spot['symbol']}, {hh.get('years_listed')} years of history, 5y CAGR "
                            f"{hh.get('cagr_5y_pct')}%, worst drawdown {hh.get('max_drawdown_all_time_pct')}%, "
                            f"{hh.get('off_all_time_high_pct')}% from its all-time high.", data={"dossier": spot})
        if user_msgs:
            try:
                memory.update_client_profile()
            except Exception:  # noqa: BLE001
                pass
        client = memory.client_block()
        self.ticks = getattr(self, "ticks", 0) + 1
        if self.ticks % 4 == 0:
            try:
                memory.update_floor_digest(sid)
            except Exception:  # noqa: BLE001
                pass
        digest = memory.floor_digest(sid)
        rulings = [{"market": s.get("market"), "mode": s.get("mode"), **{k: (s["verdict"] or {}).get(k)
                    for k in ("decision", "why")}} for s in store.sessions(15)
                   if s["verdict"] and not s["id"].startswith("floor-")][:3]
        try:
            lessons = [x["lesson"] for x in learning.active_lessons()][:6]
        except Exception:  # noqa: BLE001
            lessons = []
        just_said = "\n".join(f"CLIENT: {m['text']}" for m in user_msgs)
        for _ in range(self.speakers):
            seat = next(self.bench)
            convo = "\n".join(f"{'CLIENT' if e['kind'] == 'user' else e['speaker']}: {e['text']}"
                              for e in store.last_events(sid, 12) if e["kind"] in ("chat", "ruling", "brief", "user"))
            prompt = (f"FLOOR DATA\n{data}\n\nGOAL AND COSTS\n{json.dumps(self.goal_block, default=str)}\n\n"
                      f"SPOTLIGHT\n{json.dumps(spot, default=str)[:5000] if spot else '(none)'}\n\n"
                      f"{client or 'CLIENT PROFILE\n(nothing yet)\n\n'}"
                      f"TODAY SO FAR\n{json.dumps(digest) if digest else '(just started)'}\n\n"
                      f"RECENT COUNCIL RULINGS\n{json.dumps(rulings)}\n\n"
                      f"LESSONS THE COUNCIL HAS LEARNED\n{json.dumps(lessons)}\n\n"
                      f"CONVERSATION SO FAR\n{convo or '(you open the floor)'}\n\n"
                      + (f"THE CLIENT JUST SAID\n{just_said}\n\n" if just_said else "") + "Your turn.")
            system = f"{FLOOR_RULES}\n\nYou are {seat['name']}, the {seat['role']}. Lens: {seat['focus']}."
            for model in [seat["model"], *seat.get("fallback", [])]:
                try:
                    r = llm.chat(model, system, prompt, max_tokens=1500, timeout=90)
                except llm.LLMError:
                    continue
                d = llm.parse_json(r["text"])
                d = d if isinstance(d, dict) and d.get("message") else {"message": _salvage(r["text"])}
                store.add_event(sid, "floor", seat["name"], "chat", d["message"], model, d)
                break
            if self.stop:
                return

    # ---- scheduled councils ---------------------------------------------------------------------
    def due_council(self) -> dict | None:
        for e in self.schedule:
            now = datetime.now(market.mkt(e["market"] if e["market"] != "BOTH" else "US")["tz"])
            key = f"{now:%Y-%m-%d} {e['key']}"
            mins = (now.hour * 60 + now.minute) - (e["hh"] * 60 + e["mm"])
            if now.weekday() < 5 and 0 <= mins < 30 and key not in self.ran \
                    and now.weekday() in e.get("days", range(7)):
                return {**e, "run_key": key}
        return None

    def run_council(self, e: dict) -> None:
        self.ran.add(e["run_key"])
        sid = self.ensure_session()
        # A month-end horizon is days away late in a month, which grades noise; scheduled councils judge to
        # the end of next month instead.
        c = Council(budget=self.budget, rounds=1, mode=e["mode"], market_code=e["market"],
                    horizon="end of next month")
        store.add_event(sid, "floor", "Moderator", "status",
                        f"Full council convened: {e['mode']} {e['market']} ({e['key']}), session {c.id}. "
                        f"Codex rulings left today: {llm.budget_left('codex')}.")
        try:
            ruling = c.run()
            store.add_event(sid, "floor", "Council", "ruling",
                            f"Council ruling ({e['mode']} {e['market']}): {ruling['decision']}. {ruling.get('why', '')}",
                            data=ruling)
        except Exception as ex:  # noqa: BLE001
            store.add_event(sid, "floor", "Moderator", "error", f"Council failed: {ex}")

    # ---- learning and publishing ----------------------------------------------------------------
    def housekeeping(self) -> None:
        now = time.time()
        if now - self.last_grade > 3600:
            self.last_grade = now
            try:
                learning.grade()
            except Exception as ex:  # noqa: BLE001
                store.add_event(self.ensure_session(), "floor", "Moderator", "error", f"grading failed: {ex}")
        day = datetime.now(market.ET).strftime("%Y-%m-%d")
        if day != self.last_reflect_day and datetime.now(market.ET).hour >= 17:
            self.last_reflect_day = day
            try:
                new = learning.reflect()
                if new:
                    store.add_event(self.ensure_session(), "floor", "Post-mortem", "chat",
                                    "New lessons from graded calls: " + " | ".join(x["lesson"] for x in new),
                                    data={"lessons": new})
            except Exception as ex:  # noqa: BLE001
                store.add_event(self.ensure_session(), "floor", "Moderator", "error", f"reflection failed: {ex}")
        if day != getattr(self, "last_congress_day", "") and datetime.now(market.ET).hour >= 18:
            self.last_congress_day = day
            try:  # the Clerk posts new filings during the day; read them once each evening
                from . import congress
                r = congress.sync(years=[datetime.now(market.ET).year], max_scanned=20, progress=lambda m: None)
                if r["trades"]:
                    store.add_event(self.ensure_session(), "floor", "Research desk", "brief",
                                    f"New Congress disclosures: {r['filings']} filings, {r['trades']} trades.")
            except Exception as ex:  # noqa: BLE001
                store.add_event(self.ensure_session(), "floor", "Moderator", "error", f"congress sync failed: {ex}")
        if self.publish and now - self.last_publish > 900:
            self.last_publish = now
            try:
                from . import publish
                publish.publish()
            except Exception as ex:  # noqa: BLE001
                store.add_event(self.ensure_session(), "floor", "Moderator", "error", f"publish failed: {ex}")

    # ---- main loop ------------------------------------------------------------------------------
    def any_market_open(self) -> bool:
        return market.market_clock("US")["market_open"] or market.market_clock("IN")["market_open"]

    def run(self) -> None:
        store.fail_stale_sessions()
        self.last_user_seq = max([e["seq"] for e in store.last_events(self.ensure_session(), 50)
                                  if e["kind"] == "user"], default=0)
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stop", True))
        while not self.stop:
            e = self.due_council()
            if e:
                self.run_council(e)
            try:
                self.tick()
            except Exception as ex:  # noqa: BLE001
                store.add_event(self.ensure_session(), "floor", "Moderator", "error", f"floor tick failed: {ex}")
            self.housekeeping()
            wait = self.interval_open if self.any_market_open() else self.interval_closed
            end = time.time() + wait
            while time.time() < end and not self.stop and not self.due_council() and not self.new_user_messages():
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
        if v is True:
            args.append(f"--{k.replace('_', '-')}")
        elif v not in (None, False):
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
