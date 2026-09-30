"""The council: seats, the debate protocol, and the chair's ruling.

Three modes share one protocol:
  pick    which stock to buy with the budget, in the US or Indian market (ends in a paper position)
  ipo     which open, upcoming or just-listed IPOs to apply for, avoid, or buy after listing
  topic   any question the client brings ("Is the Tata Motors demerger good for holders?")

Protocol
  0. Research desk builds a brief for the mode and market.
  1. Opening: every seat takes a position in parallel, from its own specialty.
  2. Cross-examination: every seat reads the table and challenges or backs other seats by name.
     Runs `rounds` times.
  3. Vote.
  4. Ruling: the chair weighs votes and dissent and rules. The chair is a rationed subscription seat.
  5. pick only: a volatility band for the budget, computed, not guessed, and a paper position.
"""
from __future__ import annotations

import json
import math
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path

from importlib import resources

from . import costs, dossier
from . import ipo as ipo_desk
from . import learning, llm, market, store

PLAYBOOK = resources.files("wallstreet_council").joinpath("playbook.md").read_text()

DEFAULT_SEATS = [
    {"name": "Gemini", "model": "gemini/gemini-3.5-flash", "fallback": ["gemini/gemini-3.1-flash-lite"],
     "role": "Macro strategist",
     "focus": "rates, central banks (Fed, RBI), currencies, oil, geopolitics, flows and sector rotation"},
    {"name": "Nemotron Super", "model": "nvidia/nvidia/nemotron-3-super-120b-a12b", "role": "Quant technician",
     "focus": "trend, momentum, RSI, moving averages, volume surges, volatility and entry timing"},
    {"name": "Kimi K3", "model": "nvidia/moonshotai/kimi-k3", "fallback": ["nvidia/nvidia/nemotron-3-ultra-550b-a55b"],
     "role": "Fundamentals analyst",
     "focus": "valuation, growth, margins, analyst targets, earnings dates, IPO pricing versus listed peers"},
    {"name": "GPT-OSS", "model": "nvidia/openai/gpt-oss-20b", "role": "News and sentiment analyst",
     "focus": "headline sentiment, narrative shifts, catalysts in the last 48 hours, subscription and GMP buzz"},
    {"name": "Nemotron Ultra", "model": "nvidia/nvidia/nemotron-3-ultra-550b-a55b",
     "fallback": ["nvidia/nvidia/nemotron-3-super-120b-a12b"], "role": "Risk manager",
     "focus": "what can go wrong: drawdown, gap risk, earnings landmines, crowded trades, and when CASH wins"},
    {"name": "Muse", "model": "nvidia/meta/muse-glimmer-30b",
     "fallback": ["nvidia/nvidia/nemotron-3-super-120b-a12b"], "role": "Alternative data and culture scout",
     "focus": "sports (IPL, NFL, cricket), entertainment, festivals, consumer trends and social buzz"},
    {"name": "GLM", "model": "nvidia/z-ai/glm-5.3", "fallback": ["nvidia/deepseek-ai/deepseek-v4.1-flash"],
     "role": "Contrarian trader",
     "focus": "fading the crowd, mean reversion, oversold quality, overbought hype and IPO frenzies"},
    {"name": "Codex", "model": "codex/default", "fallback": ["nvidia/nvidia/nemotron-3-ultra-550b-a55b"],
     "role": "Chair and chief strategist",
     "focus": "weighing every argument, spotting weak reasoning, and making the final call", "chair": True},
]
CLAUDE_SEAT = {"name": "Claude", "model": "claude/opus", "role": "Devil's advocate",
               "focus": "stress-testing the strongest consensus and checking every number against the brief"}

SEATS_FILE = Path(os.environ.get("COUNCIL_SEATS", Path.home() / ".wallstreet-council" / "seats.json"))
MODES = ("pick", "ipo", "topic")
DEFAULT_BUDGET = {"US": 10.0, "IN": 1000.0, "BOTH": 10.0}

BASE_RULES = """You sit on the Wall Street Council, a panel of AI analysts running a PAPER-TRADING SIMULATION.
No real money moves. Your job is to argue well, not to sound confident.
- Use only numbers that appear in the BRIEF. Never invent prices, earnings, dates, subscription figures or GMPs.
- Short-horizon returns are mostly noise. Say so when it is true. Doing nothing (CASH, AVOID, NO) is always legal.
- Returns only count AFTER costs and taxes. Use the COSTS block: broker charges, depository charges, securities
  transaction tax, stamp duty, GST, and the capital-gains tax that applies to the holding period. Name the account
  type you assume (delivery, intraday, US zero-commission). Tax rates depend on residency; say so, never guess it.
- The client's GOAL (in COSTS.goal) is ambitious. Weigh every route to it honestly (compounding, concentrated
  stocks, IPOs, intraday, options, leveraged products, penny stocks) with its realistic odds and what the client
  loses in the common bad case. Never pretend a 10x is likely; show the arithmetic.
- The CLIENT NOTES are the client speaking to you. Address them directly when they are relevant.
- Reply with ONE JSON object and nothing else."""


def load_seats(include_claude: bool = False) -> list[dict]:
    seats = json.loads(SEATS_FILE.read_text()) if SEATS_FILE.exists() else [dict(s) for s in DEFAULT_SEATS]
    if include_claude and not any(s["model"].startswith("claude/") for s in seats):
        seats.append(dict(CLAUDE_SEAT))
    return seats


class Council:
    def __init__(self, budget: float | None = None, horizon: str = "end of this month",
                 tickers: list[str] | None = None, rounds: int = 1, include_claude: bool = False,
                 seats: list[dict] | None = None, open_paper_position: bool = True, mode: str = "pick",
                 market_code: str = "US", topic: str | None = None, target: float | None = None):
        self.id = uuid.uuid4().hex[:10]
        self.mode = mode if mode in MODES else "pick"
        self.market = market_code.upper() if market_code.upper() in ("US", "IN", "BOTH") else "US"
        if self.mode == "pick" and self.market == "BOTH":
            self.market = "US"
        self.topic = (topic or "").strip() or None
        if self.mode == "topic" and not self.topic:
            raise ValueError("topic mode needs a topic")
        self.budget = float(budget) if budget else DEFAULT_BUDGET[self.market]
        # The client's stated goal is to turn the budget into ten times as much; 0 disables it.
        self.target = self.budget * 10 if target is None else (float(target) or None)
        self.horizon, self.tickers, self.rounds = horizon, tickers or [], rounds
        self.seats = seats or load_seats(include_claude)
        self.open_paper_position = open_paper_position
        self.brief: dict = {}
        self.brief_text = ""
        store.create_session(self.id, self.budget, horizon, self.seats, mode=self.mode, market=self.market,
                             topic=self.topic)

    # ---- helpers --------------------------------------------------------------------------------
    @property
    def primary(self) -> str:
        return "IN" if self.market == "IN" else "US"

    def money(self, x: float) -> str:
        return market.money(x, self.primary)

    def rules(self) -> str:
        m = market.mkt(self.primary)
        extra = []
        if self.mode == "pick":
            extra.append(f"- Market: {m['name']}. Pick only from the CANDIDATES list.")
            extra.append("- Fractional shares are assumed, so share price does not matter." if m["fractional"] else
                         "- NSE/BSE trade WHOLE shares only. whole_shares_affordable says how many the budget buys; "
                         "a stock with 0 affordable shares cannot be picked.")
            extra.append(f"- Entry windows are in {m['tz_label']} during regular hours ({m['open']:%H:%M} to "
                         f"{m['close']:%H:%M}) on a trading day.")
        if self.mode == "ipo" or (self.topic and "ipo" in self.topic.lower()):
            for code in (["IN", "US"] if self.market == "BOTH" else [self.primary]):
                extra.append("- " + ipo_desk.RULES[code])
        if self.mode == "topic":
            extra.append("- Answer the client's question directly. If the brief lacks the data to answer, say what is "
                         "missing instead of guessing.")
        return BASE_RULES + "\n" + "\n".join(extra)

    def persona(self, seat: dict) -> str:
        if not hasattr(self, "_memory"):
            self._memory = {}
        if seat["name"] not in self._memory:
            try:
                self._memory[seat["name"]] = learning.memory_for(seat["name"])
            except Exception:  # noqa: BLE001
                self._memory[seat["name"]] = ""
        return (f"{self.rules()}\n\n{PLAYBOOK}\n\nYou are {seat['name']}, the council's {seat['role']}. "
                f"Your lens: {seat['focus']}.\n\n{self._memory[seat['name']]}")

    def say(self, phase: str, speaker: str, kind: str, text: str, model: str = "", data: dict | None = None):
        store.add_event(self.id, phase, speaker, kind, text, model, data)

    def ask(self, seat: dict, prompt: str, phase: str, max_tokens: int = 3000) -> dict | None:
        r, errs = None, []
        for model in [seat["model"], *seat.get("fallback", [])]:
            try:
                r = llm.chat(model, self.persona(seat), prompt, max_tokens=max_tokens)
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
                r2 = llm.chat(seat_model, self.persona(seat),
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

    def run_seats(self, phase: str, kind: str, prompt: str, seats: list[dict], text_of=None,
                  max_tokens: int = 3000) -> dict[str, dict]:
        def run(seat):
            d = self.ask(seat, prompt, phase, max_tokens)
            if d:
                if text_of:
                    d = text_of(d) or d
                self.say(phase, seat["name"], kind, d.get("message") or d.get("_text", ""),
                         d.get("_model", seat["model"]), d)
            return seat["name"], d
        return {n: d for n, d in self.fan_out(seats, run, phase) if d}

    # ---- research -------------------------------------------------------------------------------
    def research(self):
        say = lambda m: self.say("research", "Research desk", "status", m)  # noqa: E731
        say(f"Opening the research desk: mode {self.mode}, market {self.market}")
        edition = "IN" if self.market == "IN" else "US"
        if self.mode == "pick":
            self.brief = market.build_brief(self.tickers, progress=say, code=self.primary, budget=self.budget)
        else:
            codes = ["IN", "US"] if self.market == "BOTH" else [self.primary]
            say("Reading the tape: " + " and ".join(market.mkt(c)["name"] for c in codes))
            regime, sectors = {}, {}
            for c in codes:
                r, s = market.regime_and_sectors(c)
                regime.update(r)
                sectors.update({f"{c} {k}": v for k, v in s.items()})
            self.brief = {"clock": market.market_clock(self.primary), "regime": regime, "sectors": sectors}
            if self.mode == "ipo" or (self.topic and "ipo" in self.topic.lower()):
                self.brief["ipos"], self.brief["ipo_news"] = ipo_desk.build(codes, progress=say)
            if self.topic:
                say(f"Searching the news for: {self.topic}")
                queries = [self.topic] + [f"{t} stock" for t in self.tickers]
                with ThreadPoolExecutor(6) as ex:
                    news = {}
                    for ed in (["IN", "US"] if self.market == "BOTH" else [edition]):
                        news.update(dict(zip([f"{ed}: {q}" for q in queries],
                                             ex.map(lambda q: market.headlines(q, 8, "7d", ed), queries))))
                self.brief["topic_news"] = news
            syms = list(dict.fromkeys(self.tickers + self._tickers_in_topic()))
            if syms:
                say("Pulling price history and fundamentals for " + ", ".join(syms))
                with ThreadPoolExecutor(8) as ex:
                    self.brief["candidates"] = [d for d in ex.map(lambda s: market.candidate(s, edition), syms)
                                                if not d.get("error")]
                    code = "IN" if self.market in ("IN", "BOTH") else "US"
                    self.brief["dossiers"] = [d for d in ex.map(
                        lambda s: dossier.build(s, "IN" if s.endswith((".NS", ".BO")) else code), syms)
                        if not d.get("error")]
        end = market.horizon_end(self.horizon)
        clock = self.brief["clock"]
        clock.update({"horizon": self.horizon, "horizon_end": str(end),
                      "trading_days_to_horizon": market.trading_days_between(
                          datetime.now(market.mkt(self.primary)["tz"]).date(), end),
                      "client_budget": self.money(self.budget)})
        if self.topic:
            clock["client_question"] = self.topic
        self.brief["costs"] = costs.brief_block(self.primary, self.budget, self.target)
        self.brief_text = market.compact(self.brief)
        n_ipo = sum(len(v.get(k, [])) for v in self.brief.get("ipos", {}).values()
                    for k in ("open_now", "upcoming", "recently_listed", "priced_this_month"))
        self.say("research", "Research desk", "brief",
                 f"Brief ready ({len(self.brief_text):,} chars): {len(self.brief.get('candidates', []))} stocks"
                 + (f", {n_ipo} IPOs" if n_ipo else "") + f". {clock['now_local']}, market "
                 f"{'OPEN' if clock['market_open'] else 'CLOSED'}, {clock['trading_days_to_horizon']} trading days "
                 f"to the horizon ({clock['horizon_end']}).", data={"clock": clock, "regime": self.brief.get("regime")})

    def _tickers_in_topic(self) -> list[str]:
        """Uppercase words in the topic that resolve to a listed symbol (NSE first for India)."""
        if not self.topic:
            return []
        out = []
        for w in dict.fromkeys(re.findall(r"\b[A-Z][A-Z&]{1,11}\b", self.topic)):
            if w in {"IPO", "IPOS", "GMP", "RBI", "SEBI", "FII", "DII", "NSE", "BSE", "US", "USA", "AI", "CEO",
                     "ETF", "GDP", "CPI", "FED", "EV", "IT", "SME", "NRI", "OPT"}:
                continue
            for sym in ([f"{w}.NS", w] if self.market in ("IN", "BOTH") else [w]):
                if not market.snapshot(sym).get("error"):
                    out.append(sym)
                    break
        return out[:6]

    def client_notes(self) -> str:
        """What the client has said: in this session, and recently on the trading floor."""
        from .floor import floor_id
        mine = [e for e in store.last_events(self.id, 60) if e["kind"] == "user"]
        floor = [e for e in store.last_events(floor_id(), 80) if e["kind"] == "user"][-8:]
        notes = [f"- {e['text']}" for e in floor + mine]
        return ("CLIENT NOTES\n" + "\n".join(notes) + "\n\n") if notes else ""

    def head(self) -> str:
        return f"BRIEF\n{self.brief_text}\n\n{self.client_notes()}"

    # ---- per-mode prompt pieces -----------------------------------------------------------------
    def task(self) -> str:
        if self.mode == "pick":
            return (f"The client has {self.money(self.budget)} and wants the best return by {self.horizon}. From your "
                    "lens, pitch up to 2 picks from CANDIDATES (or CASH).\n"
                    'JSON: {"message": "what you say to the table, 3 to 6 sentences, cite brief numbers", '
                    '"market_view": "one line", "picks": [{"ticker": "", "conviction": 1-10, "thesis": "", '
                    '"catalysts": [""], "risks": [""], "entry_window": "e.g. 2026-10-01 10:00-10:30", '
                    '"account": "delivery / intraday / US zero-commission", '
                    '"horizon_return_pct_after_costs": {"bear": 0, "base": 0, "bull": 0}}], '
                    '"path_to_goal": "one or two sentences on how (or whether) this helps reach the GOAL"}')
        if self.mode == "ipo":
            focus = f" The client is especially asking: {self.topic}." if self.topic else ""
            return ("Debate the IPOs in the brief (open now, upcoming, and recently listed)." + focus + " Pick at most "
                    "4 that matter and give each a call: APPLY (bid in the IPO), AVOID, BUY_AFTER_LISTING, or WATCH.\n"
                    'JSON: {"message": "what you say to the table, 3 to 6 sentences, cite brief numbers", '
                    '"calls": [{"ipo": "company name", "call": "APPLY|AVOID|BUY_AFTER_LISTING|WATCH", '
                    '"conviction": 1-10, "why": "", "listing_return_pct": {"bear": 0, "base": 0, "bull": 0}, '
                    '"can_budget_afford_one_lot": "yes/no/unknown, from the brief", '
                    '"if_allotted": "sell on listing day or hold, and why", '
                    '"if_not_allotted": "what to do instead"}]}')
        return (f"The client asks: \"{self.topic}\". Take a clear position from your lens.\n"
                'JSON: {"message": "what you say to the table, 3 to 6 sentences, cite brief evidence", '
                '"position": "a short answer label, e.g. BUY, AVOID, YES, NO, BULLISH, BEARISH", '
                '"conviction": 1-10, "key_evidence": [""], "risks": [""]}')

    def current_view_schema(self) -> str:
        return {"pick": '{"answer": "TICKER or CASH", "conviction": 1-10, "why": ""}',
                "ipo": '{"answer": "your top call, e.g. APPLY Srit India", "conviction": 1-10, "why": ""}',
                "topic": '{"answer": "your position label", "conviction": 1-10, "why": ""}'}[self.mode]

    # ---- protocol -------------------------------------------------------------------------------
    def opening(self) -> dict[str, dict]:
        self.say("opening", "Moderator", "status", "Round 1: opening positions. Every seat speaks at once.")
        seats = [s for s in self.seats if not s.get("chair")] or self.seats
        return self.run_seats("opening", "pitch", f"{self.head()}TASK: {self.task()}", seats)

    def deep_dive(self, state: dict[str, dict]) -> None:
        """Pull the full file on every stock a seat pitched, then narrow the brief to those names."""
        named: list[str] = []
        for d in state.values():
            for p in d.get("picks") or []:
                if isinstance(p, dict) and p.get("ticker"):
                    named.append(str(p["ticker"]).upper().strip())
        known = {c["symbol"] for c in self.brief.get("candidates", [])}
        named = [t for t in dict.fromkeys(named) if t in known][:8]
        if not named:
            return
        self.say("research", "Research desk", "status",
                 "Pulling the full file on every pitched stock: " + ", ".join(named)
                 + " (entire price history, drawdowns, yearly returns, quarterly results, earnings reactions, "
                   "analyst moves, insider trades)")
        with ThreadPoolExecutor(8) as ex:
            files = [d for d in ex.map(lambda t: dossier.build(t, self.primary), named) if not d.get("error")]
        self.brief["dossiers"] = files
        # From here on the table argues over the pitched names in depth, not all 30 in brief.
        self.brief_text = market.compact(self.brief, only=set(named))
        h = {f["symbol"]: f.get("history", {}) for f in files}
        self.say("research", "Research desk", "brief", "Dossiers ready. " + "; ".join(
            f"{k}: {v.get('years_listed')}y listed, 5y CAGR {v.get('cagr_5y_pct')}%, worst drawdown "
            f"{v.get('max_drawdown_all_time_pct')}%" for k, v in h.items()), data={"dossiers": files})

    def cross_exam(self, state: dict[str, dict], rnd: int) -> dict[str, dict]:
        table = json.dumps({n: {k: d.get(k) for k in ("message", "picks", "calls", "position")
                                if d.get(k) is not None} for n, d in state.items()}, default=str)[:24000]
        prompt = (f"{self.head()}WHAT THE TABLE SAID (round {rnd + 1})\n{table}\n\n"
                  "TASK: Respond to the other seats BY NAME. Challenge the weakest argument with evidence from the "
                  "brief, back the strongest one, and say whether you changed your mind.\n"
                  'JSON: {"message": "4 to 7 sentences addressed to named seats", "challenges": [{"to": "seat name", '
                  f'"point": ""}}], "backs": ["seat name"], "changed_mind": true/false, "current_view": '
                  f'{self.current_view_schema()}}}')
        self.say("debate", "Moderator", "status", f"Round {rnd + 1}: cross-examination.")
        out = self.run_seats("debate", "rebuttal", prompt, [s for s in self.seats if s["name"] in state])
        return {n: {**state[n], "rebuttal": out.get(n)} for n in state}

    def vote(self, state: dict[str, dict]) -> dict[str, dict]:
        convo = json.dumps({n: {"opening": d.get("message"), "rebuttal": (d.get("rebuttal") or {}).get("message"),
                                "current_view": (d.get("rebuttal") or {}).get("current_view")}
                            for n, d in state.items()}, default=str)[:24000]
        seats = [s for s in self.seats if s["name"] in state]
        head = f"{self.head()}DEBATE SO FAR\n{convo}\n\nTASK: Cast your final vote. "
        if self.mode == "pick":
            prices = {c["symbol"]: c for c in self.brief["candidates"]}
            prompt = head + (f"Client budget {self.money(self.budget)}, horizon {self.horizon}. One ticker or CASH.\n"
                             'JSON: {"vote": "TICKER or CASH", "confidence": 1-10, "entry_window": "", '
                             '"horizon_return_pct": {"bear": 0, "base": 0, "bull": 0}, "reason": "2 to 3 sentences"}')

            def fix(d):
                t = str(d.get("vote", "CASH")).upper().strip()
                ok = t in prices and (prices[t].get("whole_shares_affordable", 1) > 0)
                d["vote"] = t if ok else "CASH"
                d["price_at_vote"] = prices[d["vote"]]["price"] if ok else None
                d["message"] = f"I vote {d['vote']} (confidence {d.get('confidence')}/10). {d.get('reason', '')}"
                return d
        elif self.mode == "ipo":
            prompt = head + ("One call per IPO you have a view on (at most 4), and your single best idea.\n"
                             'JSON: {"votes": [{"ipo": "company name", "call": "APPLY|AVOID|BUY_AFTER_LISTING|WATCH", '
                             '"confidence": 1-10}], "best": "company name or NONE", "reason": "2 to 3 sentences"}')

            def fix(d):
                d["votes"] = [v for v in (d.get("votes") or []) if isinstance(v, dict) and v.get("ipo")]
                calls = "; ".join(f"{v['ipo']}: {str(v.get('call', '')).upper()} ({v.get('confidence')})"
                                  for v in d["votes"])
                d["message"] = f"My calls: {calls or 'none'}. Best: {d.get('best', 'NONE')}. {d.get('reason', '')}"
                return d
        else:
            prompt = head + ("Answer the client's question in a short label.\n"
                             'JSON: {"vote": "short answer label, e.g. YES, NO, BUY, AVOID", "confidence": 1-10, '
                             '"reason": "2 to 3 sentences"}')

            def fix(d):
                d["vote"] = str(d.get("vote", "UNSURE")).upper().strip()[:40]
                d["message"] = f"I vote {d['vote']} (confidence {d.get('confidence')}/10). {d.get('reason', '')}"
                return d
        self.say("vote", "Moderator", "status", "Final vote.")
        return self.run_seats("vote", "vote", prompt, seats, text_of=fix, max_tokens=1500)

    def tally(self, votes: dict[str, dict]) -> dict[str, float]:
        """Confidence times the seat's learned trust weight (1.0 until it has a graded record)."""
        t: dict[str, float] = {}
        try:
            trust = learning.weights()
        except Exception:  # noqa: BLE001
            trust = {}

        def add(k, c, seat):
            try:
                w = float(c)
            except (TypeError, ValueError):
                w = 5.0
            t[k] = round(t.get(k, 0) + w * trust.get(seat, 1.0), 2)
        for seat, d in votes.items():
            if self.mode == "ipo":
                for v in d.get("votes", []):
                    add(f"{v['ipo']}: {str(v.get('call', '')).upper()}", v.get("confidence"), seat)
            else:
                add(d["vote"], d.get("confidence"), seat)
        return dict(sorted(t.items(), key=lambda x: -x[1]))

    def ruling(self, votes: dict[str, dict]) -> dict:
        tally = self.tally(votes)
        self.say("ruling", "Moderator", "tally", "Tally (confidence x each seat's learned trust weight): " +
                 ", ".join(f"{k} {v:g}" for k, v in tally.items()), data=tally)
        chair = next((s for s in self.seats if s.get("chair")), None)
        # The chair sits on a rationed subscription seat, so it gets a slim brief.
        if self.mode == "pick":
            slim = market.compact({**self.brief, "dossiers": [f for f in self.brief.get("dossiers", [])
                                                              if f["symbol"] in tally]}, only=set(tally), news=False)
            ask = ('JSON: {"decision": "TICKER or CASH", "entry_window": "", "why": "4 to 6 sentences", '
                   '"dissent": "", "stop_loss_pct": 0, "take_profit_pct": 0, '
                   '"horizon_return_pct": {"bear": 0, "base": 0, "bull": 0}, "confidence": 1-10, '
                   '"what_would_change_our_mind": ""}')
        elif self.mode == "ipo":
            slim = market.compact({"clock": self.brief["clock"], "ipos": self.brief.get("ipos")}, news=False)[:9000]
            ask = ('JSON: {"rulings": [{"ipo": "", "call": "APPLY|AVOID|BUY_AFTER_LISTING|WATCH", "why": ""}], '
                   '"decision": "the single best IPO idea, or NONE", "why": "4 to 6 sentences", "dissent": "", '
                   '"confidence": 1-10, "what_would_change_our_mind": ""}')
        else:
            slim = self.brief_text[:9000]
            ask = ('JSON: {"decision": "the council\'s answer in a few words", "why": "4 to 6 sentences", '
                   '"dissent": "", "confidence": 1-10, "what_would_change_our_mind": ""}')
        slim_votes = {n: {k: v.get(k) for k in ("vote", "votes", "best", "confidence", "entry_window",
                                                 "horizon_return_pct", "reason") if v.get(k) is not None}
                      for n, v in votes.items()}
        question = {"pick": f"the client's {self.money(self.budget)} for {self.horizon}",
                    "ipo": "which IPOs to apply for, avoid, or buy after listing" +
                           (f" (client's focus: {self.topic})" if self.topic else ""),
                    "topic": f'the client\'s question: "{self.topic}"'}[self.mode]
        prompt = (f"BRIEF\n{slim}\n\nCOSTS {json.dumps(self.brief.get('costs'), default=str)[:3500]}\n\n"
                  f"{self.client_notes()}VOTES\n{json.dumps(slim_votes, default=str)[:6000]}\n\nTALLY {json.dumps(tally)}"
                  f"\n\nTASK: As chair, rule on {question}. You may overrule the tally if the reasoning behind it is "
                  f"weak, but say why. Name the dissent.\n{ask}")
        d = None
        if chair:
            self.say("ruling", chair["name"], "status", "The chair is deliberating")
            d = self.ask(chair, prompt, "ruling", max_tokens=2500)
        if not d:  # chair missing or failed: the tally decides
            top = next(iter(tally), "CASH" if self.mode == "pick" else "NO CONSENSUS")
            d = {"decision": top, "why": "Chair unavailable, so the confidence-weighted tally decides.",
                 "confidence": None}
        if self.mode == "pick":
            prices = {c["symbol"] for c in self.brief["candidates"]}
            dec = str(d.get("decision", "CASH")).upper().strip()
            d["decision"] = dec if dec in prices else "CASH"
        d["decision"] = str(d.get("decision") or "NONE")
        d["tally"], d["mode"], d["market"], d["topic"] = tally, self.mode, self.market, self.topic
        self.say("ruling", chair["name"] if chair else "Moderator", "ruling",
                 f"RULING: {d['decision']}. {d.get('why', '')}", d.get("_model", chair["model"]) if chair else "", d)
        return d

    def quant_check(self, d: dict) -> dict:
        """What the volatility alone says the budget could be worth at the horizon. Not a forecast."""
        c = next((x for x in self.brief["candidates"] if x["symbol"] == d["decision"]), None)
        end = market.horizon_end(self.horizon)
        days = market.trading_days_between(datetime.now(market.mkt(self.primary)["tz"]).date(), end)
        b, fmt = self.budget, self.money
        if not c or not c.get("vol_annual_pct"):
            q = {"note": "CASH: the budget stays at its value", "value_mid": b}
        else:
            sig = c["vol_annual_pct"] / 100 * math.sqrt(max(days, 1) / 252)
            q = {"price": c["price"], "trading_days": days, "sigma_horizon_pct": round(sig * 100, 2),
                 "value_1sd_low": round(b * math.exp(-sig), 2), "value_mid": round(b, 2),
                 "value_1sd_high": round(b * math.exp(sig), 2), "value_2sd_low": round(b * math.exp(-2 * sig), 2),
                 "value_2sd_high": round(b * math.exp(2 * sig), 2),
                 "note": "About two thirds of outcomes fall inside the 1-sd band if volatility holds. "
                         "The drift over a few weeks is too small to estimate honestly, so the middle is flat."}
            prof = "IN-delivery-zero-brokerage" if self.primary == "IN" else "US-zero-commission"
            rt = costs.round_trip(prof, b, price=c["price"])
            q["round_trip_charges"] = rt["total_charges"]
            q["breakeven_move_pct"] = rt["breakeven_move_pct"]
            for k in ("value_1sd_low", "value_mid", "value_1sd_high", "value_2sd_low", "value_2sd_high"):
                q[k] = round(q[k] - rt["total_charges"], 2)
            q["note"] += (f" Values are after about {fmt(rt['total_charges'])} of round-trip charges ({prof}); "
                          "capital-gains tax on any profit comes on top.")
            base = (d.get("horizon_return_pct") or {}).get("base")
            if isinstance(base, (int, float)):
                q["council_base_value"] = round(b * (1 + base / 100), 2)
        self.say("quant", "Quant check", "quant",
                 (f"{fmt(b)} in {d['decision']} by {end} ({days} trading days): 1-sd range "
                  f"{fmt(q['value_1sd_low'])} to {fmt(q['value_1sd_high'])}, 2-sd {fmt(q['value_2sd_low'])} to "
                  f"{fmt(q['value_2sd_high'])}, after {fmt(q['round_trip_charges'])} of charges (breakeven move "
                  f"{q['breakeven_move_pct']}%)." if "value_1sd_low" in q else q["note"]), data=q)
        return q

    def paper_position(self, ruling: dict) -> None:
        m = market.mkt(self.primary)
        price = market.price_now(ruling["decision"])
        shares = self.budget / price if m["fractional"] else math.floor(self.budget / price)
        if shares <= 0:
            self.say("close", "Moderator", "paper", f"{ruling['decision']} costs {self.money(price)} a share, more "
                     f"than the {self.money(self.budget)} budget, so no paper position was opened.")
            return
        invested = shares * price
        pid = store.open_position(self.id, ruling["decision"], invested, price, str(market.horizon_end(self.horizon)),
                                  shares=shares, currency=m["currency"])
        ruling["paper_position"] = {"id": pid, "entry_price": round(price, 4), "shares": round(shares, 6),
                                    "invested": round(invested, 2), "currency": m["currency"]}
        self.say("close", "Moderator", "paper",
                 f"Paper position #{pid}: {self.money(invested)} of {ruling['decision']} at {self.money(price)} "
                 f"({shares:.4g} shares). Simulation only, nothing was bought.", data=ruling["paper_position"])

    def run(self) -> dict:
        try:
            what = {"pick": f"a {market.mkt(self.primary)['name']} pick for {self.money(self.budget)}",
                    "ipo": f"IPOs ({self.market})", "topic": f'"{self.topic}"'}[self.mode]
            self.say("start", "Moderator", "status",
                     f"Council convened on {what} with {len(self.seats)} seats: "
                     + ", ".join(f"{s['name']} ({s['role']})" for s in self.seats))
            self.research()
            state = self.opening()
            if not state:
                raise RuntimeError("no seat produced an opening; check keys and model availability")
            if self.mode == "pick":
                self.deep_dive(state)
            for r in range(self.rounds):
                state = self.cross_exam(state, r)
            votes = self.vote(state)
            ruling = self.ruling(votes)
            if self.mode == "pick":
                ruling["quant"] = self.quant_check(ruling)
                if self.open_paper_position and ruling["decision"] != "CASH":
                    self.paper_position(ruling)
                chair = next((s["name"] for s in self.seats if s.get("chair")), "Council")
                learning.record_calls(self.id, self.primary, str(market.horizon_end(self.horizon)), votes, ruling,
                                      chair, {c["symbol"]: c["price"] for c in self.brief["candidates"]})
                self.say("close", "Moderator", "status", "Every vote and the ruling are now on the record and will be "
                         "graded against the " + ("Nifty 50" if self.primary == "IN" else "S&P 500") + ".")
            store.finish_session(self.id, "done", ruling)
            self.say("end", "Moderator", "status", "Council adjourned.")
            return ruling
        except Exception as e:  # noqa: BLE001
            store.finish_session(self.id, "failed", error=str(e))
            self.say("end", "Moderator", "error", f"Council failed: {e}")
            raise
