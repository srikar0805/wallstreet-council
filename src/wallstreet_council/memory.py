"""Long-term memory, so nothing the client said scrolls away while the models talk to each other.

  Client profile   every client message ever sent is kept, and a free-tier model folds each new one into a
                   standing profile: goals, constraints, accounts, preferences, decisions, and OPEN QUESTIONS
                   the council still owes an answer to. Every floor turn and every council reads the profile,
                   plus the client's recent messages verbatim.
  Floor digest     every few floor rounds, the day's conversation is condensed into theses, agreements,
                   disagreements and a watchlist, so the floor builds on itself instead of restarting.

Never uses a rationed (subscription) seat.
"""
from __future__ import annotations

import json
import time

from . import llm, store

MODELS = ["nvidia/nvidia/nemotron-3-super-120b-a12b", "nvidia/nvidia/nemotron-3-ultra-550b-a55b",
          "gemini/gemini-3.1-flash-lite"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory (key TEXT PRIMARY KEY, ts REAL, value TEXT, upto_seq INTEGER);
"""
EMPTY_PROFILE = {"goals": [], "constraints": [], "accounts_and_location": [], "preferences": [],
                 "decisions": [], "open_questions": [], "answered_questions": []}


def _conn():
    c = store.conn()
    c.executescript(SCHEMA)
    return c


def _get(key: str) -> tuple[dict | None, int]:
    with _conn() as c:
        r = c.execute("SELECT value, upto_seq FROM memory WHERE key=?", (key,)).fetchone()
    return (json.loads(r["value"]), r["upto_seq"] or 0) if r else (None, 0)


def _put(key: str, value: dict, upto_seq: int) -> None:
    with store._lock, _conn() as c:
        c.execute("INSERT INTO memory (key, ts, value, upto_seq) VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                  "ts=excluded.ts, value=excluded.value, upto_seq=excluded.upto_seq",
                  (key, time.time(), json.dumps(value), upto_seq))


def _ask(system: str, prompt: str) -> dict | None:
    for model in MODELS:
        try:
            d = llm.parse_json(llm.chat(model, system, prompt, max_tokens=2500, timeout=120)["text"])
        except llm.LLMError:
            continue
        if isinstance(d, dict):
            return d
    return None


def client_profile() -> dict:
    return _get("client_profile")[0] or dict(EMPTY_PROFILE)


def update_client_profile() -> dict:
    """Fold every client message not yet seen into the profile."""
    profile, upto = _get("client_profile")
    profile = profile or dict(EMPTY_PROFILE)
    with store.conn() as c:
        new = [dict(r) for r in c.execute("SELECT seq, ts, text FROM events WHERE kind='user' AND seq>? ORDER BY seq",
                                          (upto,)).fetchall()]
    if not new:
        return profile
    with store.conn() as c:
        recent_answers = [r["text"] for r in c.execute(
            "SELECT text FROM events WHERE kind='chat' AND seq>? ORDER BY seq DESC LIMIT 15",
            (new[0]["seq"],)).fetchall()][::-1]
    prompt = (f"CURRENT PROFILE\n{json.dumps(profile)}\n\nNEW CLIENT MESSAGES\n"
              + "\n".join(f"- {m['text']}" for m in new)
              + f"\n\nWHAT THE COUNCIL SAID SINCE\n" + "\n".join(f"- {t[:400]}" for t in recent_answers)
              + "\n\nTASK: Update the client's profile. Keep every durable fact the client stated (goals, budget, "
                "constraints, accounts, country, preferences, decisions). Add each new question to open_questions; "
                "move a question to answered_questions (with a one-line answer) only if the council clearly answered "
                "it above. Never invent facts about the client. Short phrases.\n"
                f"JSON with exactly these keys: {json.dumps(list(EMPTY_PROFILE))}")
    d = _ask("You maintain a client's profile for an investment research council. JSON only.", prompt)
    if d:
        profile = {k: d.get(k, profile.get(k, [])) for k in EMPTY_PROFILE}
        answered = " ".join(str(a) for a in profile["answered_questions"])
        profile["open_questions"] = [q for q in profile["open_questions"] if str(q)[:60] not in answered]
        _put("client_profile", profile, new[-1]["seq"])
    return profile


def floor_digest(sid: str) -> dict:
    return _get(f"digest:{sid}")[0] or {}


def update_floor_digest(sid: str) -> dict:
    digest, upto = _get(f"digest:{sid}")
    evs = [e for e in store.events(sid, upto, 500) if e["kind"] in ("chat", "user", "ruling")]
    if len(evs) < 6:
        return digest or {}
    convo = "\n".join(f"{'CLIENT' if e['kind'] == 'user' else e['speaker']}: {e['text'][:500]}" for e in evs)[-24000:]
    prompt = (f"DIGEST SO FAR\n{json.dumps(digest or {})}\n\nNEW CONVERSATION\n{convo}\n\n"
              "TASK: Update the running digest of today's trading-floor conversation. Keep it tight: the theses "
              "argued (with who holds them), where the seats agree, where they disagree, stocks and IPOs on the "
              "watchlist and why, and what the client asked and was told. Drop stale points. Use only facts stated "
              "in the conversation.\n"
              'JSON: {"theses": [""], "agreements": [""], "disagreements": [""], "watchlist": [{"ticker": "", "why": ""}], '
              '"client_threads": [""]}')
    d = _ask("You keep minutes for a trading desk. JSON only.", prompt)
    if d:
        _put(f"digest:{sid}", d, evs[-1]["seq"])
        return d
    return digest or {}


def client_block(recent: int = 8) -> str:
    """What every prompt gets about the client."""
    p = client_profile()
    msgs = store.user_messages(recent)
    has = any(p.get(k) for k in EMPTY_PROFILE)
    if not has and not msgs:
        return ""
    out = []
    if has:
        out.append("CLIENT PROFILE (everything the client has told us, kept permanently)\n" + json.dumps(p))
    if msgs:
        out.append("CLIENT'S LATEST MESSAGES\n" + "\n".join(f"- {m['text']}" for m in msgs))
    return "\n\n".join(out) + "\n\n"


def seed(facts: dict) -> dict:
    """Merge facts the client stated elsewhere (for example to the operator) into the profile."""
    profile, upto = _get("client_profile")
    profile = profile or dict(EMPTY_PROFILE)
    for k, v in facts.items():
        profile[k] = list(dict.fromkeys(profile.get(k, []) + v))
    _put("client_profile", profile, upto)
    return profile


def plain_summary(sid: str, max_age: int = 3600, context: str = "") -> dict:
    """Three short sentences a non-investor understands, about what the floor is discussing. Cached hourly."""
    cached, _ = _get(f"plain:{sid}")
    if cached and time.time() - cached.get("ts", 0) < max_age:
        return cached
    evs = [e for e in store.last_events(sid, 40) if e["kind"] in ("chat", "user", "ruling")]
    if not evs:
        return cached or {}
    convo = "\n".join(f"{'CLIENT' if e['kind'] == 'user' else e['speaker']}: {e['text'][:600]}" for e in evs)[-16000:]
    d = _ask("You explain finance to someone's parent who has never traded. JSON only.",
             f"CURRENT FACTS (these override anything older in the conversation)\n{context}\n\n"
             f"CONVERSATION\n{convo}\n\nTASK: Summarise what these analysts are discussing right now for a reader "
             "who does not know finance. At most 3 short sentences, everyday words, no jargon or abbreviations (no "
             "RSI, CAGR, STCG, GMP, beta, drawdown), name companies in full, include at most one number per sentence. "
             "The first sentence must state the latest picks exactly as given in CURRENT FACTS (never contradict "
             "them). The other sentences say what the analysts are discussing now. Only facts from CURRENT FACTS "
             'and the conversation.\nJSON: {"summary": ""}')
    if d and d.get("summary"):
        out = {"summary": d["summary"], "ts": time.time()}
        _put(f"plain:{sid}", out, evs[-1]["seq"])
        return out
    return cached or {}
