"""The council: seats, the debate protocol, and the chair's ruling.

Protocol
  0. Research desk builds a brief (market.build_brief).
  1. Opening: every seat pitches picks in parallel, from its own specialty.
  2. Cross-examination: every seat reads all pitches and challenges or backs other seats by name.
     Runs `rounds` times.
  3. Vote: every seat casts one vote (a ticker or CASH) with an entry window.
  4. Ruling: the chair weighs the votes and the dissent and issues one decision.
  5. Quant check: a volatility band for the budget at month end, computed, not guessed.
  6. A paper position is opened at the current price. Nothing is ever sent to a broker.
"""
from __future__ import annotations

import json
import math
import os
import uuid
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

from . import llm, market, store

DEFAULT_SEATS = [
    {"name": "Gemini", "model": "gemini/gemini-3.5-flash", "fallback": ["gemini/gemini-3.1-flash-lite"], "role": "Macro strategist",
     "focus": "rates, the Fed, the dollar, oil, geopolitics, sector rotation and what the regime favours"},
    {"name": "Nemotron Super", "model": "nvidia/nvidia/nemotron-3-super-120b-a12b", "role": "Quant technician",
     "focus": "trend, momentum, RSI, moving averages, volume surges, volatility and entry timing"},
    {"name": "Kimi K3", "model": "nvidia/moonshotai/kimi-k3", "fallback": ["nvidia/nvidia/nemotron-3-ultra-550b-a55b"],
     "role": "Fundamentals analyst",
     "focus": "valuation, growth, margins, analyst targets, earnings dates and balance sheet risk"},
    {"name": "GPT-OSS", "model": "nvidia/openai/gpt-oss-20b", "role": "News and sentiment analyst",
     "focus": "headline sentiment, narrative shifts, catalysts in the last 48 hours and crowd positioning"},
    {"name": "Nemotron Ultra", "model": "nvidia/nvidia/nemotron-3-ultra-550b-a55b",
     "fallback": ["nvidia/nvidia/nemotron-3-super-120b-a12b"], "role": "Risk manager",
     "focus": "what can go wrong: drawdown, gap risk, earnings landmines, crowded trades, and when CASH wins"},
    {"name": "Muse", "model": "nvidia/meta/muse-glimmer-30b",
     "fallback": ["nvidia/nvidia/nemotron-3-super-120b-a12b"], "role": "Alternative data and culture scout",
     "focus": "sports, entertainment, consumer trends, social buzz and events that move specific names"},
    {"name": "GLM", "model": "nvidia/z-ai/glm-5.3", "fallback": ["nvidia/deepseek-ai/deepseek-v4.1-flash"],
     "role": "Contrarian trader",
     "focus": "fading the crowd, mean reversion, oversold quality and overbought hype"},
    {"name": "Codex", "model": "codex/default", "fallback": ["nvidia/nvidia/nemotron-3-ultra-550b-a55b"],
     "role": "Chair and chief strategist",
     "focus": "weighing every argument, spotting weak reasoning, and making the final call", "chair": True},
]
CLAUDE_SEAT = {"name": "Claude", "model": "claude/opus", "role": "Devil's advocate",
               "focus": "stress-testing the strongest consensus and checking every number against the brief"}

SEATS_FILE = Path(os.environ.get("COUNCIL_SEATS", Path.home() / ".wallstreet-council" / "seats.json"))

RULES = """You sit on the Wall Street Council, a panel of AI analysts running a PAPER-TRADING SIMULATION.
No real money moves. Your job is to argue well, not to sound confident.
Rules:
- Use only numbers that appear in the BRIEF. Never invent prices, earnings, dates or statistics.
- US equities only, from the CANDIDATES list. Fractional shares are assumed, so price does not matter.
- Short-horizon stock returns are mostly noise. Say so when it is true. CASH is always a legal answer.
- Entry windows are in US Eastern time during regular hours (09:30 to 16:00) on a trading day.
- Reply with ONE JSON object and nothing else."""


def load_seats(include_claude: bool = False) -> list[dict]:
    seats = json.loads(SEATS_FILE.read_text()) if SEATS_FILE.exists() else [dict(s) for s in DEFAULT_SEATS]
    if include_claude and not any(s["model"].startswith("claude/") for s in seats):
        seats.append(dict(CLAUDE_SEAT))
    return seats


def _persona(seat: dict) -> str:
    return f"{RULES}\n\nYou are {seat['name']}, the council's {seat['role']}. Your lens: {seat['focus']}."


class Council:
    def __init__(self, budget: float = 10.0, horizon: str = "end of this month", tickers: list[str] | None = None,
                 rounds: int = 1, include_claude: bool = False, seats: list[dict] | None = None,
                 open_paper_position: bool = True):
        self.id = uuid.uuid4().hex[:10]
        self.budget, self.horizon, self.tickers, self.rounds = budget, horizon, tickers or [], rounds
        self.seats = seats or load_seats(include_claude)
        self.open_paper_position = open_paper_position
        self.brief: dict = {}
        self.brief_text = ""
        store.create_session(self.id, budget, horizon, self.seats)

    # ---- plumbing -------------------------------------------------------------------------------
    def say(self, phase: str, speaker: str, kind: str, text: str, model: str = "", data: dict | None = None):
        store.add_event(self.id, phase, speaker, kind, text, model, data)

    def ask(self, seat: dict, prompt: str, phase: str, max_tokens: int = 3000) -> dict | None:
        r, errs = None, []
        for model in [seat["model"], *seat.get("fallback", [])]:
            try:
                r = llm.chat(model, _persona(seat), prompt, max_tokens=max_tokens)
                break
            except Exception as e:  # noqa: BLE001
                errs.append(f"{model}: {e}"[:200])
        if r is None:
            self.say(phase, seat["name"], "error", "could not answer: " + " | ".join(errs), seat["model"])
            return None
        if r["model"] != seat["model"]:
            self.say(phase, seat["name"], "status", f"{seat['model']} unavailable, sitting in with {r['model']}")
        seat_model = r["model"]
        data = llm.parse_json(r["text"])
        if not isinstance(data, dict) and seat_model.split("/")[0] not in ("codex", "claude"):
            # one repair attempt: reasoning models sometimes spend the budget thinking aloud and never
            # reach the JSON, so ask again with their notes attached and a larger budget
            try:
                r2 = llm.chat(seat_model, _persona(seat),
                              f"{prompt}\n\nYOUR DRAFT NOTES\n{r['text'][-6000:]}\n\nNow output ONLY the final JSON "
                              "object. No analysis, no preamble, no code fence.", max_tokens=max_tokens * 2)
                data = llm.parse_json(r2["text"])
            except Exception:  # noqa: BLE001
                data = None
        if not isinstance(data, dict):
            self.say(phase, seat["name"], "error", "answered without usable JSON: " + r["text"][:600], seat_model)
            return None
        data["_ms"], data["_model"] = r["ms"], seat_model
        return data

    def fan_out(self, seats: list[dict], fn, phase: str, deadline: int = 300):
        """Run every seat at once. Seats still talking at the deadline are skipped for this phase,
        so one slow model cannot stall the table."""
        ex = ThreadPoolExecutor(len(seats) or 1)
        futs = {ex.submit(fn, s): s for s in seats}
        done, late = wait(futs, timeout=deadline)
        ex.shutdown(wait=False, cancel_futures=True)
        for f in late:
            self.say(phase, futs[f]["name"], "status", f"ran past the {deadline}s phase deadline and was skipped")
        return [f.result() for f in done if not f.exception()]

    # ---- protocol -------------------------------------------------------------------------------
    def research(self):
        self.say("research", "Research desk", "status", "Opening the research desk")
        self.brief = market.build_brief(self.tickers, progress=lambda m: self.say("research", "Research desk",
                                                                                  "status", m))
        end = market.horizon_end(self.horizon)
        self.brief["clock"]["horizon"] = self.horizon
        self.brief["clock"]["horizon_end"] = str(end)
        self.brief["clock"]["trading_days_to_horizon"] = market.trading_days_between(
            datetime.now(market.ET).date(), end)
        self.brief_text = market.compact(self.brief)
        c = self.brief["clock"]
        self.say("research", "Research desk", "brief",
                 f"Brief ready: {len(self.brief['candidates'])} candidates. Now {c['now_et']}, market "
                 f"{'OPEN' if c['market_open'] else 'CLOSED'}, {c['trading_days_to_horizon']} trading days "
                 f"to the horizon ({c['horizon_end']}).", data={"clock": c, "regime": self.brief["regime"],
                                                "sectors": self.brief["sectors"],
                                                "tickers": [x["symbol"] for x in self.brief["candidates"]]})

    def opening(self) -> dict[str, dict]:
        prompt = (f"BRIEF\n{self.brief_text}\n\nTASK: The client has ${self.budget:.2f} and wants the best return by "
                  f"{self.horizon}. From your lens, pitch up to 2 picks from CANDIDATES (or CASH).\n"
                  'JSON: {"message": "what you say to the table, 3 to 6 sentences, cite brief numbers", '
                  '"market_view": "one line", "picks": [{"ticker": "", "conviction": 1-10, "thesis": "", '
                  '"catalysts": [""], "risks": [""], "entry_window_et": "e.g. 2026-09-30 10:00-10:30", '
                  '"horizon_return_pct": {"bear": 0, "base": 0, "bull": 0}}]}')
        self.say("opening", "Moderator", "status", "Round 1: opening pitches. Every seat speaks at once.")
        seats = [s for s in self.seats if not s.get("chair")] or self.seats

        def run(seat):
            d = self.ask(seat, prompt, "opening")
            if d:
                self.say("opening", seat["name"], "pitch", d.get("message", ""), d.get("_model", seat["model"]), d)
            return seat["name"], d
        return {n: d for n, d in self.fan_out(seats, run, "opening") if d}

    def cross_exam(self, pitches: dict[str, dict], rnd: int) -> dict[str, dict]:
        table = json.dumps({n: {"message": d.get("message"), "picks": d.get("picks")} for n, d in pitches.items()},
                           default=str)[:24000]
        prompt = (f"BRIEF\n{self.brief_text}\n\nWHAT THE TABLE SAID (round {rnd})\n{table}\n\n"
                  "TASK: Respond to the other seats BY NAME. Challenge the weakest argument with a number from the "
                  "brief, back the strongest one, and say whether you changed your mind.\n"
                  'JSON: {"message": "4 to 7 sentences addressed to named seats", "challenges": [{"to": "seat name", '
                  '"point": ""}], "backs": ["seat name"], "changed_mind": true/false, "current_pick": '
                  '{"ticker": "or CASH", "conviction": 1-10, "why": ""}}')
        self.say("debate", "Moderator", "status", f"Round {rnd + 1}: cross-examination.")
        seats = [s for s in self.seats if s["name"] in pitches]

        def run(seat):
            d = self.ask(seat, prompt, "debate")
            if d:
                self.say("debate", seat["name"], "rebuttal", d.get("message", ""), d.get("_model", seat["model"]), d)
            return seat["name"], d
        out = {n: d for n, d in self.fan_out(seats, run, "debate") if d}
        return {n: {**pitches[n], "rebuttal": out.get(n)} for n in pitches}

    def vote(self, state: dict[str, dict]) -> dict[str, dict]:
        convo = json.dumps({n: {"pitch": d.get("message"), "rebuttal": (d.get("rebuttal") or {}).get("message"),
                                "current_pick": (d.get("rebuttal") or {}).get("current_pick")}
                            for n, d in state.items()}, default=str)[:24000]
        prompt = (f"BRIEF\n{self.brief_text}\n\nDEBATE SO FAR\n{convo}\n\nTASK: Cast your final vote for the client's "
                  f"${self.budget:.2f}, horizon {self.horizon}. One ticker or CASH.\n"
                  'JSON: {"vote": "TICKER or CASH", "confidence": 1-10, "entry_window_et": "", '
                  '"horizon_return_pct": {"bear": 0, "base": 0, "bull": 0}, "reason": "2 to 3 sentences"}')
        self.say("vote", "Moderator", "status", "Final vote. One ticker or CASH per seat.")
        seats = [s for s in self.seats if s["name"] in state]
        prices = {c["symbol"]: c["price"] for c in self.brief["candidates"]}

        def run(seat):
            d = self.ask(seat, prompt, "vote", max_tokens=1500)
            if d:
                t = str(d.get("vote", "CASH")).upper().strip()
                d["vote"] = t if t in prices or t == "CASH" else "CASH"
                d["price_at_vote"] = prices.get(d["vote"])
                self.say("vote", seat["name"], "vote",
                         f"I vote {d['vote']} (confidence {d.get('confidence')}/10). {d.get('reason', '')}",
                         d.get("_model", seat["model"]), d)
            return seat["name"], d
        return {n: d for n, d in self.fan_out(seats, run, "vote") if d}

    def ruling(self, votes: dict[str, dict], state: dict[str, dict]) -> dict:
        tally: dict[str, float] = {}
        for d in votes.values():
            tally[d["vote"]] = tally.get(d["vote"], 0) + float(d.get("confidence") or 5)
        self.say("ruling", "Moderator", "tally", "Confidence-weighted tally: " +
                 ", ".join(f"{k} {v:g}" for k, v in sorted(tally.items(), key=lambda x: -x[1])), data=tally)
        chair = next((s for s in self.seats if s.get("chair")), None)
        # The chair sits on a rationed subscription seat, so it gets a slim brief: regime, sectors and only
        # the tickers someone voted for, plus the votes. The full pitches already shaped those votes.
        slim = market.compact(self.brief, only=set(tally), news=False)
        slim_votes = {n: {k: v.get(k) for k in ("vote", "confidence", "entry_window_et", "horizon_return_pct",
                                                 "reason")} for n, v in votes.items()}
        prompt = (f"BRIEF\n{slim}\n\nVOTES\n{json.dumps(slim_votes, default=str)[:6000]}\n\n"
                  f"TALLY {json.dumps(tally)}\n\n"
                  f"TASK: As chair, rule on the client's ${self.budget:.2f} for {self.horizon}. You may overrule the "
                  "tally if the reasoning behind it is weak, but say why. Name the dissent.\n"
                  'JSON: {"decision": "TICKER or CASH", "entry_window_et": "", "why": "4 to 6 sentences", '
                  '"dissent": "", "stop_loss_pct": 0, "take_profit_pct": 0, '
                  '"horizon_return_pct": {"bear": 0, "base": 0, "bull": 0}, "confidence": 1-10, '
                  '"what_would_change_our_mind": ""}')
        d = None
        if chair:
            self.say("ruling", chair["name"], "status", "The chair is deliberating")
            d = self.ask(chair, prompt, "ruling", max_tokens=2500)
        if not d:  # chair missing or failed: the tally decides
            top = max(tally, key=tally.get) if tally else "CASH"
            d = {"decision": top, "why": "Chair unavailable, so the confidence-weighted tally decides.",
                 "confidence": None}
        prices = {c["symbol"]: c for c in self.brief["candidates"]}
        dec = str(d.get("decision", "CASH")).upper().strip()
        d["decision"] = dec if dec in prices else "CASH"
        d["tally"] = tally
        self.say("ruling", chair["name"] if chair else "Moderator", "ruling",
                 f"RULING: {d['decision']}. {d.get('why', '')}", d.get("_model", chair["model"]) if chair else "", d)
        return d

    def quant_check(self, d: dict) -> dict:
        """What the volatility alone says $budget could be worth at month end. Not a forecast."""
        c = next((x for x in self.brief["candidates"] if x["symbol"] == d["decision"]), None)
        end = market.horizon_end(self.horizon)
        days = market.trading_days_between(datetime.now(market.ET).date(), end)
        if not c or not c.get("vol_annual_pct"):
            q = {"note": "CASH: the budget stays at its value", "value_mid": self.budget}
        else:
            sig = c["vol_annual_pct"] / 100 * math.sqrt(max(days, 1) / 252)
            q = {"price": c["price"], "trading_days": days, "sigma_horizon_pct": round(sig * 100, 2),
                 "value_1sd_low": round(self.budget * math.exp(-sig), 2),
                 "value_mid": round(self.budget, 2),
                 "value_1sd_high": round(self.budget * math.exp(sig), 2),
                 "value_2sd_low": round(self.budget * math.exp(-2 * sig), 2),
                 "value_2sd_high": round(self.budget * math.exp(2 * sig), 2),
                 "note": "About two thirds of outcomes fall inside the 1-sd band if volatility holds. "
                         "The drift over a few weeks is too small to estimate honestly, so the middle is flat."}
            base = (d.get("horizon_return_pct") or {}).get("base")
            if isinstance(base, (int, float)):
                q["council_base_value"] = round(self.budget * (1 + base / 100), 2)
        self.say("quant", "Quant check", "quant",
                 (f"${self.budget:.2f} in {d['decision']} by {end} ({days} trading days): 1-sd range "
                  f"${q['value_1sd_low']} to ${q['value_1sd_high']}, 2-sd ${q['value_2sd_low']} to ${q['value_2sd_high']}."
                  if "value_1sd_low" in q else q["note"]), data=q)
        return q

    def run(self) -> dict:
        try:
            self.say("start", "Moderator", "status",
                     f"Council convened with {len(self.seats)} seats: "
                     + ", ".join(f"{s['name']} ({s['role']})" for s in self.seats))
            self.research()
            state = self.opening()
            if not state:
                raise RuntimeError("no seat produced a pitch; check keys and model availability")
            for r in range(self.rounds):
                state = self.cross_exam(state, r)
            votes = self.vote(state)
            ruling = self.ruling(votes, state)
            ruling["quant"] = self.quant_check(ruling)
            if self.open_paper_position and ruling["decision"] != "CASH":
                price = market.price_now(ruling["decision"])
                pid = store.open_position(self.id, ruling["decision"], self.budget, price,
                                          str(market.horizon_end(self.horizon)))
                ruling["paper_position"] = {"id": pid, "entry_price": round(price, 4),
                                            "shares": round(self.budget / price, 6)}
                self.say("close", "Moderator", "paper",
                         f"Paper position #{pid}: ${self.budget:.2f} of {ruling['decision']} at ${price:.2f} "
                         f"({self.budget / price:.4f} shares). Simulation only, nothing was bought.",
                         data=ruling["paper_position"])
            store.finish_session(self.id, "done", ruling)
            self.say("end", "Moderator", "status", "Council adjourned.")
            return ruling
        except Exception as e:  # noqa: BLE001
            store.finish_session(self.id, "failed", error=str(e))
            self.say("end", "Moderator", "error", f"Council failed: {e}")
            raise
